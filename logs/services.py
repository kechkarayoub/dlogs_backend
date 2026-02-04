"""Services for calculating distances and generating HOS (Hours of Service) driving steps.

This module provides utilities for:
- Calculating distances between coordinates using OpenRouteService API
- Generating DOT-compliant driving schedules based on HOS regulations
"""
from datetime import datetime, timedelta

from django.conf import settings
import requests


class DistaneCalculator:
    """Calculates distances between coordinates using OpenRouteService API.
    
    Attributes:
        endpoint (str): The OpenRouteService API endpoint URL
        headers (dict): HTTP headers including API authorization
    """
    
    def __init__(self, endpoint):
        """Initialize the distance calculator.
        
        Args:
            endpoint (str): The OpenRouteService API endpoint URL
        """
        self.endpoint = endpoint
        self.headers = {
            'Authorization': settings.OPENROUTESERVICE_API_KEY,
            'Content-Type': 'application/json'
        }
    
    def calculate_distance(self, start_coords, pickup_coords, dropoff_coords):
        """Calculate total distance across multiple waypoints.
        
        Args:
            start_coords (list): Starting coordinates [latitude, longitude]
            pickup_coords (list): Pickup location coordinates [latitude, longitude]
            dropoff_coords (list): Dropoff location coordinates [latitude, longitude]
            
        Returns:
            requests.Response: API response containing distance information
        """
        body = {"coordinates": [start_coords, pickup_coords, dropoff_coords]}
        response = requests.post(self.endpoint, json=body, headers=self.headers)
        return response


class StepsGenerator:
    """Generates HOS (Hours of Service) compliant driving steps for truck drivers.
    
    This class respects DOT regulations:
    - 14-hour on-duty window
    - 11-hour driving limit per day
    - 10-hour break requirement
    - 8-day cycle resets
    
    Attributes:
        cycle_remaining (float): Remaining hours in current 168-hour cycle
    """
    
    def __init__(self, cycle_used_hrs=0):
        """Initialize the steps generator.
        
        Args:
            cycle_used_hrs (float, optional): Hours already used in current cycle. Defaults to 0.
        """
        # Calculate remaining hours in current 168-hour (8-day) cycle
        self.cycle_remaining = settings.MAX_CYCLE_HOURS - float(cycle_used_hrs)
    
    def _create_segment(self, status, duration, label, start_hour, elapsed_start, miles_moved=0):
        """Create a single segment of the driving schedule.
        
        Args:
            status (str): Duty status (DRIVING, ON_DUTY, OFF_DUTY, SLEEPER)
            duration (float): Duration in hours
            label (str): Human-readable description of the segment
            start_hour (int): Starting hour of the day (0-23)
            elapsed_start (float): Total elapsed hours from start
            miles_moved (float, optional): Miles traveled in this segment. Defaults to 0.
            
        Returns:
            dict: Segment dictionary with timing and status information
        """
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
        """Create a segment, splitting across midnight if necessary.
        
        This method handles segments that span multiple days by splitting them
        into separate segments at the midnight boundary.
        
        Args:
            status (str): Duty status (DRIVING, ON_DUTY, OFF_DUTY, SLEEPER)
            duration (float): Duration in hours
            label (str): Human-readable description
            start_hour (int): Starting hour of the day (0-23)
            elapsed_start (float): Total elapsed hours from start
            steps (list): List to append segment to
            miles_moved (float, optional): Miles to distribute. Defaults to 0.
            
        Returns:
            dict: The segment dictionary for the portion after midnight (if split), or the full segment
        """
        rest_of_days = elapsed_start % 24
        if rest_of_days + duration > 24:  # Segment spans midnight
            # Split segment into two parts: before and after midnight
            first_part_duration = 24 - rest_of_days
            first_part_miles = (miles_moved * first_part_duration) / duration if miles_moved else 0
            segment1 = self._create_segment(status, first_part_duration, label, start_hour, elapsed_start, miles_moved=first_part_miles)
            steps.append(segment1)
            # Create second part starting at midnight
            new_elapsed_start = elapsed_start + first_part_duration
            second_part_duration = duration - first_part_duration
            segment2 = self._create_segment(status, second_part_duration, label, 0, new_elapsed_start, miles_moved=miles_moved - first_part_miles)
            return segment2
        else:
            # Segment fits within same day
            segment = self._create_segment(status, duration, label, start_hour, elapsed_start, miles_moved=miles_moved)
            return segment
        

    def generate_steps(self, dist_to_pickup_meters, dist_to_dropoff_meters):
        """Generate a complete HOS-compliant driving schedule.
        
        Creates a detailed timeline of activities (driving, on-duty, breaks, sleep) that
        complies with DOT Hours of Service regulations from start through dropoff.
        
        Args:
            dist_to_pickup_meters (float): Distance from start to pickup in meters
            dist_to_dropoff_meters (float): Distance from pickup to dropoff in meters
            
        Returns:
            list: List of segments, each containing timing and status information
        """
        # Convert distances from meters to miles
        dist_to_pickup_miles = dist_to_pickup_meters * 0.000621371
        dist_to_dropoff_miles = dist_to_dropoff_meters * 0.000621371
        
        # Initialize tracking variables
        steps = []  # List of all driving segments
        current_hour = settings.START_DUTY_HOUR  # Current hour of day (0-23)
        total_elapsed = 0.0  # Total hours elapsed since start
        current_drive_window = 0.0  # Hours driven in current 14-hour window
        current_drive_accumulated = 0.0  # Total driving hours in current day
        drive_accumulated_since_last_break = 0.0  # Hours driven since last break
        miles_since_fuel = 0.0  # Miles driven since last fueling
        # Add initial OFF_DUTY segment if starting partway through the day
        if current_hour > 0:
            seg = self.manage_create_segment("OFF_DUTY", current_hour, "OFF_DUTY", 0, total_elapsed, steps)
            steps.append(seg)
            total_elapsed += current_hour
            current_hour = seg["end_hour"]
        
        # Add pre-trip inspection
        seg = self.manage_create_segment("ON_DUTY", settings.PRE_TRIP_INSPECTION_TIME, "Pre-trip Inspection", current_hour, total_elapsed, steps)
        steps.append(seg)
        current_hour = seg["end_hour"]
        total_elapsed += settings.PRE_TRIP_INSPECTION_TIME
        current_drive_window += settings.PRE_TRIP_INSPECTION_TIME
        self.cycle_remaining -= settings.PRE_TRIP_INSPECTION_TIME
        
        # Initialize distance and pickup tracking
        remaining_dist = dist_to_pickup_miles + dist_to_dropoff_miles
        pickup_done = False
        # Main driving loop - continues until all distance is covered
        while remaining_dist > 0:
            # Check if 8-day cycle limit reached - requires extended rest
            if self.cycle_remaining <= 0:
                seg = self.manage_create_segment("OFF_DUTY", settings.REST_AFTER_CYCLE, f"{settings.REST_AFTER_CYCLE}h Cycle Restart", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.REST_AFTER_CYCLE
                # Reset cycle and all driving counters
                self.cycle_remaining = settings.MAX_CYCLE_HOURS
                current_drive_window, current_drive_accumulated, drive_accumulated_since_last_break = 0, 0, 0
            
            # Calculate constraints for next driving segment
            dist_to_next_stop = dist_to_pickup_miles if not pickup_done else remaining_dist
            
            # Remaining time until hitting various limits
            left_time_driving_per_day = settings.MAX_DRIVING_PER_DAY - current_drive_accumulated  # 11-hour limit
            left_time_drive_window = settings.MAX_DRIVE_WINDOW - current_drive_window  # 14-hour window
            left_time_before_break = settings.BREAK_AFTER - ((drive_accumulated_since_last_break % settings.BREAK_AFTER) if drive_accumulated_since_last_break else 0)  # Break every 8 hours
            left_time_to_next_stop = dist_to_next_stop / settings.AVG_SPEED_MPH  # Time to reach next location
            
            # Take the minimum - most restrictive constraint
            time_to_drive = min(
                left_time_driving_per_day,
                left_time_drive_window,
                left_time_before_break,
                left_time_to_next_stop,
                self.cycle_remaining
            )

            # Add driving segment if time available
            if time_to_drive > 0:
                miles_moved = time_to_drive * settings.AVG_SPEED_MPH
                seg = self.manage_create_segment("DRIVING", time_to_drive, "Driving", current_hour, total_elapsed, steps, miles_moved=miles_moved)
                steps.append(seg)
                
                # Update distance tracking
                if not pickup_done: 
                    dist_to_pickup_miles -= miles_moved
                remaining_dist -= miles_moved
                miles_since_fuel += miles_moved
                
                # Update all time counters
                current_hour = seg["end_hour"]
                total_elapsed += time_to_drive
                current_drive_window += time_to_drive
                current_drive_accumulated += time_to_drive
                drive_accumulated_since_last_break += time_to_drive
                self.cycle_remaining -= time_to_drive
            
            # Handle arrival at pickup location
            if not pickup_done and dist_to_pickup_miles <= 0:
                seg = self.manage_create_segment("ON_DUTY", settings.PICKUP_TIME, "Pickup Loading", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.PICKUP_TIME
                current_drive_window += settings.PICKUP_TIME
                self.cycle_remaining -= settings.PICKUP_TIME
                pickup_done = True
            
            # Handle mandatory breaks and rest periods
            if miles_since_fuel >= settings.MILES_BEFORE_FUEL:
                # Fueling stop (counts as on-duty time)
                seg = self.manage_create_segment("ON_DUTY", settings.FUELING_DURATION, "Fueling", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.FUELING_DURATION
                current_drive_window += settings.FUELING_DURATION
                self.cycle_remaining -= settings.FUELING_DURATION
                miles_since_fuel = 0
            elif drive_accumulated_since_last_break >= settings.BREAK_AFTER:
                # 8-hour break required
                seg = self.manage_create_segment("OFF_DUTY", settings.BREAK_DURATION, f"{settings.BREAK_DURATION}h Break", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.BREAK_DURATION
                current_drive_window += settings.BREAK_DURATION
                drive_accumulated_since_last_break = 0
            elif (current_drive_accumulated >= settings.MAX_DRIVING_PER_DAY or current_drive_window >= settings.MAX_DRIVE_WINDOW):
                # 10-hour sleep break required (resets daily limits)
                seg = self.manage_create_segment("SLEEPER", settings.SLEEPER_BREAK_HOURS, "10-hour Sleep", current_hour, total_elapsed, steps)
                steps.append(seg)
                current_hour = seg["end_hour"]
                total_elapsed += settings.SLEEPER_BREAK_HOURS
                # Reset all daily counters after sleep
                current_drive_window, current_drive_accumulated, drive_accumulated_since_last_break = 0, 0, 0
        # Add final dropoff segment
        seg = self.manage_create_segment("ON_DUTY", settings.DROPOFF_TIME, "Drop-off Unloading", current_hour, total_elapsed, steps)
        steps.append(seg)
        current_hour = seg["end_hour"]
        total_elapsed += settings.DROPOFF_TIME

        # Add remaining OFF_DUTY time until end of day
        left_time = 24 - (current_hour % 24)
        if left_time > 0:
            seg = self._create_segment("OFF_DUTY", left_time, "OFF_DUTY", current_hour, total_elapsed)
            steps.append(seg)
            current_hour = seg["end_hour"]
            total_elapsed += left_time

        return steps
