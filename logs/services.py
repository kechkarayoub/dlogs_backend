from datetime import datetime, timedelta


from django.conf import settings

import requests


class DistaneCalculator:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.headers = {
            'Authorization': settings.OPENROUTESERVICE_API_KEY,
            'Content-Type': 'application/json'
        }
    
    def calculate_distance(self, start_coords, pickup_coords, dropoff_coords):
        body = {"coordinates": [start_coords, pickup_coords, dropoff_coords]}
        response = requests.post(self.endpoint, json=body, headers=self.headers)
        return response


class StepsGenerator:
    def __init__(self, cycle_used_hrs=0):
        # On initialise avec le cycle restant
        self.cycle_remaining = settings.MAX_CYCLE_HOURS - float(cycle_used_hrs)
    
    def _create_segment(self, status, duration, label, start_hour, elapsed_start, miles_moved=0):
        """ Crée un dictionnaire segment avec calcul des heures et jours. """
        return {
            "status": status,
            "duration": duration,
            "label": label,
            "start_hour": start_hour,
            "end_hour": (start_hour + duration) % 24,
            "elapsed_start": elapsed_start,
            "elapsed_end": elapsed_start + duration,
            "day_number": int(elapsed_start // 24) + 1,
            "miles_moved": miles_moved
        }

    def manage_create_segment(self, status, duration, label, start_hour, elapsed_start, steps, miles_moved=0):
        rest_of_days = elapsed_start % 24
        if rest_of_days + duration > 24:
            first_part_duration = 24 - rest_of_days
            first_part_miles = (miles_moved * first_part_duration) / duration if miles_moved else 0
            segment1 = self._create_segment(status, first_part_duration, label, start_hour, elapsed_start, miles_moved=first_part_miles)
            steps.append(segment1)
            new_elapsed_start = elapsed_start + first_part_duration
            second_part_duration = duration - first_part_duration
            segment2 = self._create_segment(status, second_part_duration, label, 0, new_elapsed_start, miles_moved=miles_moved - first_part_miles)
            return segment2
        else:
            segment = self._create_segment(status, duration, label, start_hour, elapsed_start, miles_moved=miles_moved)
            return segment
        

    def generate_steps(self, dist_to_pickup_meters, dist_to_dropoff_meters):
        dist_to_pickup_miles = dist_to_pickup_meters * 0.000621371
        dist_to_dropoff_miles = dist_to_dropoff_meters * 0.000621371
        steps = []
        current_hour = settings.START_DUTY_HOUR
        total_elapsed = 0.0
        current_drive_window = 0.0
        current_drive_accumulated = 0.0
        drive_accumulated_since_last_break = 0.0
        miles_since_fuel = 0.0
        if current_hour > 0:
            seg = self.manage_create_segment("OFF_DUTY", current_hour, "OFF_DUTY", 0, total_elapsed, steps)
            steps.append(seg)
            total_elapsed += current_hour
            current_hour = seg["end_hour"]
        seg = self.manage_create_segment("ON_DUTY", settings.PRE_TRIP_INSPECTION_TIME, "Pre-trip Inspection", current_hour, total_elapsed, steps)
        steps.append(seg)
        current_hour = seg["end_hour"]
        total_elapsed += settings.PRE_TRIP_INSPECTION_TIME
        current_drive_window += settings.PRE_TRIP_INSPECTION_TIME
        self.cycle_remaining -= settings.PRE_TRIP_INSPECTION_TIME
        remaining_dist = dist_to_pickup_miles + dist_to_dropoff_miles
        pickup_done = False
        while remaining_dist > 0:
            if self.cycle_remaining <= 0:
                seg = self.manage_create_segment("OFF_DUTY", settings.REST_AFTER_CYCLE, f"{settings.REST_AFTER_CYCLE}h Cycle Restart", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.REST_AFTER_CYCLE
                self.cycle_remaining = settings.MAX_CYCLE_HOURS
                current_drive_window, current_drive_accumulated, drive_accumulated_since_last_break = 0, 0, 0
            dist_to_next_stop = dist_to_pickup_miles if not pickup_done else remaining_dist
            left_time_driving_per_day = settings.MAX_DRIVING_PER_DAY - current_drive_accumulated
            left_time_drive_window = settings.MAX_DRIVE_WINDOW - current_drive_window
            left_time_before_break = settings.BREAK_AFTER - ((drive_accumulated_since_last_break % settings.BREAK_AFTER) if drive_accumulated_since_last_break else 0)
            left_time_to_next_stop = dist_to_next_stop / settings.AVG_SPEED_MPH
            time_to_drive = min(
                left_time_driving_per_day,
                left_time_drive_window,
                left_time_before_break,
                left_time_to_next_stop,
                self.cycle_remaining
            )

            if time_to_drive > 0:
                miles_moved = time_to_drive * settings.AVG_SPEED_MPH
                seg = self.manage_create_segment("DRIVING", time_to_drive, "Driving", current_hour, total_elapsed, steps, miles_moved=miles_moved)
                steps.append(seg)
                if not pickup_done: dist_to_pickup_miles -= miles_moved
                remaining_dist -= miles_moved
                miles_since_fuel += miles_moved
                current_hour = seg["end_hour"]
                total_elapsed += time_to_drive
                current_drive_window += time_to_drive
                current_drive_accumulated += time_to_drive
                drive_accumulated_since_last_break += time_to_drive
                self.cycle_remaining -= time_to_drive
            if not pickup_done and dist_to_pickup_miles <= 0:
                seg = self.manage_create_segment("ON_DUTY", settings.PICKUP_TIME, "Pickup Loading", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.PICKUP_TIME
                current_drive_window += settings.PICKUP_TIME
                self.cycle_remaining -= settings.PICKUP_TIME
                pickup_done = True
            if miles_since_fuel >= settings.MILES_BEFORE_FUEL:
                seg = self.manage_create_segment("ON_DUTY", settings.FUELING_DURATION, "Fueling", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.FUELING_DURATION
                current_drive_window += settings.FUELING_DURATION
                self.cycle_remaining -= settings.FUELING_DURATION
                miles_since_fuel = 0
            elif drive_accumulated_since_last_break >= settings.BREAK_AFTER:
                seg = self.manage_create_segment("OFF_DUTY", settings.BREAK_DURATION, f"{settings.BREAK_DURATION}h Break", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.BREAK_DURATION
                current_drive_window += settings.BREAK_DURATION
                drive_accumulated_since_last_break = 0
            elif (current_drive_accumulated >= settings.MAX_DRIVING_PER_DAY or current_drive_window >= settings.MAX_DRIVE_WINDOW):
                seg = self.manage_create_segment("SLEEPER", settings.SLEEPER_BREAK_HOURS, "10-hour Sleep", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.SLEEPER_BREAK_HOURS
                current_drive_window, current_drive_accumulated, drive_accumulated_since_last_break = 0, 0, 0
        seg = self.manage_create_segment("ON_DUTY", settings.DROPOFF_TIME, "Drop-off Unloading", current_hour, total_elapsed, steps)
        steps.append(seg)
        current_hour = seg["end_hour"]
        total_elapsed += settings.DROPOFF_TIME

        left_time = 24 - (current_hour % 24)
        if left_time > 0:
            seg = self._create_segment("OFF_DUTY", left_time, "OFF_DUTY", current_hour, total_elapsed)
            steps.append(seg)
            current_hour = seg["end_hour"]
            total_elapsed += left_time

        return steps
