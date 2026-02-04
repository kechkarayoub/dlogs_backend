from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .services import DistaneCalculator, StepsGenerator


class LogsView(APIView):
    def post(self, request):
        
        # Frontend sends [lat, lng] but ORS expects [lng, lat], so we swap them
        start_coords = request.data.get('start')   # [lat, lng] from frontend
        pickup_coords = request.data.get('pickup') # [lat, lng] from frontend
        dropoff_coords = request.data.get('dropoff') # [lat, lng] from frontend
        cycle_used = float(request.data.get('cycle_used', 0))
        
        # Swap coordinates to [lng, lat] for OpenRouteService
        start_coords = [start_coords[1], start_coords[0]]
        pickup_coords = [pickup_coords[1], pickup_coords[0]]
        dropoff_coords = [dropoff_coords[1], dropoff_coords[0]]

        # Try driving-car first (more permissive), fallback if needed
        distance_calculator = DistaneCalculator("https://api.openrouteservice.org/v2/directions/driving-car")
        response = distance_calculator.calculate_distance(start_coords, pickup_coords, dropoff_coords)
        print(response.status_code, response.text)
        if response.status_code != 200:
            return Response({"error": "Erreur API ORS", "details": response.text}, status=400)

        data = response.json()
        route = data['routes'][0]
        
        # OpenRouteService renvoie un segment par intervalle entre deux coordonnées
        # Segment 0: Start -> Pickup
        # Segment 1: Pickup -> Dropoff
        segments = route['segments']
        
        dist_to_pickup = segments[0]['distance'] # en mètres
        dist_to_dropoff = segments[1]['distance'] # en mètres
        
        # Calcul HOS avec les deux distances séparées
        steps_generator = StepsGenerator(cycle_used)
        steps = steps_generator.generate_steps(dist_to_pickup, dist_to_dropoff)
        total_distance = dist_to_pickup + dist_to_dropoff
        return Response({
            "distance_to_pickup_meters": dist_to_pickup,
            "distance_to_dropoff_meters": dist_to_dropoff,
            "total_distance_meters": total_distance,
            "total_distance_miles": total_distance * 0.000621371,
            "route_geometry": route['geometry'], 
            "steps": steps
        })
