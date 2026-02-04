from django.test import TestCase
from unittest.mock import Mock, patch
from logs.services import DistaneCalculator, StepsGenerator
import requests


class DistanceCalculatorTests(TestCase):
    """Test suite for DistanceCalculator class."""
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.test_endpoint = "https://api.openrouteservice.org/v2/matrix/driving"
        self.calculator = DistaneCalculator(self.test_endpoint)
    
    def test_init_sets_endpoint(self):
        """Test that initialization sets the endpoint correctly."""
        self.assertEqual(self.calculator.endpoint, self.test_endpoint)
    
    def test_init_sets_headers(self):
        """Test that initialization sets authorization headers."""
        self.assertIn('Authorization', self.calculator.headers)
        self.assertEqual(self.calculator.headers['Content-Type'], 'application/json')
    
    @patch('logs.services.requests.post')
    def test_calculate_distance_calls_api(self, mock_post):
        """Test that calculate_distance makes correct API call."""
        # Setup
        start_coords = [40.7128, -74.0060]  # NYC
        pickup_coords = [40.7580, -73.9855]  # Midtown
        dropoff_coords = [40.7489, -73.9680]  # East Side
        
        mock_response = Mock()
        mock_response.json.return_value = {"distances": [[0, 1000, 2000]]}
        mock_post.return_value = mock_response
        
        # Execute
        result = self.calculator.calculate_distance(start_coords, pickup_coords, dropoff_coords)
        
        # Assert
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], self.test_endpoint)
        self.assertEqual(call_args[1]['json']['coordinates'], [start_coords, pickup_coords, dropoff_coords])
        self.assertEqual(call_args[1]['headers'], self.calculator.headers)
    
    @patch('logs.services.requests.post')
    def test_calculate_distance_returns_response(self, mock_post):
        """Test that calculate_distance returns the API response."""
        mock_response = Mock(spec=requests.Response)
        mock_post.return_value = mock_response
        
        result = self.calculator.calculate_distance([0, 0], [1, 1], [2, 2])
        
        self.assertEqual(result, mock_response)


class StepsGeneratorTests(TestCase):
    """Test suite for StepsGenerator class."""
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.generator = StepsGenerator(cycle_used_hrs=0)
    
    def test_init_sets_cycle_remaining(self):
        """Test that initialization calculates remaining cycle hours correctly."""
        generator = StepsGenerator(cycle_used_hrs=50)
        # MAX_CYCLE_HOURS is 70 (from settings)
        self.assertEqual(generator.cycle_remaining, 20.0)
    
    def test_init_default_cycle(self):
        """Test that initialization with no args starts with full cycle."""
        generator = StepsGenerator()
        from django.conf import settings
        self.assertEqual(generator.cycle_remaining, settings.MAX_CYCLE_HOURS)
    
    def test_create_segment_basic(self):
        """Test that _create_segment creates correct segment structure."""
        segment = self.generator._create_segment(
            status="DRIVING",
            duration=2.5,
            label="Test Drive",
            start_hour=10,
            elapsed_start=100,
            miles_moved=150
        )
        
        self.assertEqual(segment['status'], "DRIVING")
        self.assertEqual(segment['duration'], 2.5)
        self.assertEqual(segment['label'], "Test Drive")
        self.assertEqual(segment['start_hour'], 10)
        self.assertEqual(segment['elapsed_start'], 100)
        self.assertEqual(segment['miles_moved'], 150)
        self.assertIn('end_hour', segment)
        self.assertIn('elapsed_end', segment)
        self.assertIn('day_number', segment)
    
    def test_create_segment_calculates_day_number(self):
        """Test that day_number is calculated correctly."""
        # Day 1: 0-24 hours
        seg1 = self.generator._create_segment("ON_DUTY", 1, "Test", 10, 0)
        self.assertEqual(seg1['day_number'], 1)
        
        # Day 2: 24-48 hours
        seg2 = self.generator._create_segment("ON_DUTY", 1, "Test", 10, 24)
        self.assertEqual(seg2['day_number'], 2)
        
        # Day 3: 48-72 hours
        seg3 = self.generator._create_segment("ON_DUTY", 1, "Test", 10, 48)
        self.assertEqual(seg3['day_number'], 3)
    
    def test_create_segment_calculates_end_hour(self):
        """Test that end_hour wraps around 24-hour clock."""
        # Starting at 22:00, 4-hour duration -> 02:00 next day
        segment = self.generator._create_segment("DRIVING", 4, "Test", 22, 0)
        self.assertEqual(segment['end_hour'], 2)
        
        # Starting at 10:00, 8-hour duration -> 18:00 same day
        segment = self.generator._create_segment("DRIVING", 8, "Test", 10, 0)
        self.assertEqual(segment['end_hour'], 18)
    
    def test_manage_create_segment_no_midnight_split(self):
        """Test manage_create_segment when segment fits in same day."""
        steps = []
        segment = self.generator.manage_create_segment(
            "DRIVING", 3, "Drive", 10, 50, steps, miles_moved=200
        )
        
        # Should return segment directly without splitting
        self.assertEqual(segment['duration'], 3)
        self.assertEqual(len(steps), 0)  # No splits, so nothing appended yet
    
    def test_manage_create_segment_with_midnight_split(self):
        """Test manage_create_segment when segment crosses midnight."""
        steps = []
        # elapsed_start=22 means 22 hours have passed (22 % 24 = 22, rest of day = 22)
        # With 4-hour duration: 22 + 4 = 26, which is > 24, so it splits
        segment = self.generator.manage_create_segment(
            "DRIVING", 4, "Night Drive", 22, 22, steps, miles_moved=300
        )
        
        # Should have split into two parts
        self.assertEqual(len(steps), 1)  # First part appended
        self.assertEqual(steps[0]['duration'], 2)  # 22:00 to 24:00 = 2 hours
        
        # Returned segment is second part
        self.assertEqual(segment['duration'], 2)  # 00:00 to 02:00 = 2 hours
        self.assertEqual(segment['start_hour'], 0)
    
    def test_manage_create_segment_distributes_miles(self):
        """Test that miles are distributed correctly when splitting."""
        steps = []
        # elapsed_start=22, 4-hour drive crossing midnight with 300 miles
        # 22 + 4 = 26, which is > 24, so it splits
        segment = self.generator.manage_create_segment(
            "DRIVING", 4, "Drive", 22, 22, steps, miles_moved=300
        )
        
        # First part: 2 hours out of 4 = 150 miles
        self.assertEqual(steps[0]['miles_moved'], 150)
        # Second part: 2 hours out of 4 = 150 miles
        self.assertEqual(segment['miles_moved'], 150)
    
    @patch('django.conf.settings')
    def test_generate_steps_basic_flow(self, mock_settings):
        """Test basic generate_steps flow with minimal settings."""
        # Setup minimal mocked settings
        mock_settings.START_DUTY_HOUR = 6
        mock_settings.PRE_TRIP_INSPECTION_TIME = 0.25
        mock_settings.MAX_CYCLE_HOURS = 168
        mock_settings.MAX_DRIVING_PER_DAY = 11
        mock_settings.MAX_DRIVE_WINDOW = 14
        mock_settings.BREAK_AFTER = 8
        mock_settings.AVG_SPEED_MPH = 60
        mock_settings.PICKUP_TIME = 1
        mock_settings.DROPOFF_TIME = 0.5
        mock_settings.MILES_BEFORE_FUEL = 500
        mock_settings.FUELING_DURATION = 0.5
        mock_settings.BREAK_DURATION = 0.5
        mock_settings.SLEEPER_BREAK_HOURS = 10
        mock_settings.REST_AFTER_CYCLE = 10
        
        # 100 miles to pickup, 100 miles to dropoff = ~3.3 hours driving
        generator = StepsGenerator(cycle_used_hrs=0)
        steps = generator.generate_steps(160934, 160934)  # meters
        
        # Should generate steps
        self.assertGreater(len(steps), 0)
        self.assertIsInstance(steps, list)
        
        # First segment should be OFF_DUTY (from hour 0 to START_DUTY_HOUR)
        self.assertEqual(steps[0]['status'], 'OFF_DUTY')
        
        # Should contain DRIVING and ON_DUTY segments
        statuses = [s['status'] for s in steps]
        self.assertIn('DRIVING', statuses)
        self.assertIn('ON_DUTY', statuses)
    
    @patch('django.conf.settings')
    def test_generate_steps_includes_dropoff(self, mock_settings):
        """Test that generate_steps includes dropoff segment."""
        mock_settings.START_DUTY_HOUR = 6
        mock_settings.PRE_TRIP_INSPECTION_TIME = 0.25
        mock_settings.MAX_CYCLE_HOURS = 168
        mock_settings.MAX_DRIVING_PER_DAY = 11
        mock_settings.MAX_DRIVE_WINDOW = 14
        mock_settings.BREAK_AFTER = 8
        mock_settings.AVG_SPEED_MPH = 60
        mock_settings.PICKUP_TIME = 1
        mock_settings.DROPOFF_TIME = 0.5
        mock_settings.MILES_BEFORE_FUEL = 500
        mock_settings.FUELING_DURATION = 0.5
        mock_settings.BREAK_DURATION = 0.5
        mock_settings.SLEEPER_BREAK_HOURS = 10
        mock_settings.REST_AFTER_CYCLE = 10
        
        generator = StepsGenerator(cycle_used_hrs=0)
        steps = generator.generate_steps(80467, 80467)  # ~50 miles each
        
        # Last segments should include dropoff and final OFF_DUTY
        labels = [s['label'] for s in steps[-3:]]
        self.assertIn('Drop-off Unloading', labels)
    
    @patch('django.conf.settings')
    def test_generate_steps_respects_distance(self, mock_settings):
        """Test that all distance is covered."""
        mock_settings.START_DUTY_HOUR = 6
        mock_settings.PRE_TRIP_INSPECTION_TIME = 0.25
        mock_settings.MAX_CYCLE_HOURS = 168
        mock_settings.MAX_DRIVING_PER_DAY = 11
        mock_settings.MAX_DRIVE_WINDOW = 14
        mock_settings.BREAK_AFTER = 8
        mock_settings.AVG_SPEED_MPH = 60
        mock_settings.PICKUP_TIME = 1
        mock_settings.DROPOFF_TIME = 0.5
        mock_settings.MILES_BEFORE_FUEL = 500
        mock_settings.FUELING_DURATION = 0.5
        mock_settings.BREAK_DURATION = 0.5
        mock_settings.SLEEPER_BREAK_HOURS = 10
        mock_settings.REST_AFTER_CYCLE = 10
        
        generator = StepsGenerator(cycle_used_hrs=0)
        dist_to_pickup = 80467  # ~50 miles
        dist_to_dropoff = 80467  # ~50 miles
        steps = generator.generate_steps(dist_to_pickup, dist_to_dropoff)
        
        # Total miles driven should cover the distance
        total_miles = sum(s.get('miles_moved', 0) for s in steps)
        # Should have driven at least the required distance (accounting for rounding)
        self.assertGreater(total_miles, 95)  # ~100 miles total
