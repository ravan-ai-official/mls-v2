"""Climate hazard projections for a property coordinate."""

from __future__ import annotations

import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from mls.services.climate_risk import build_climate_risk

logger = logging.getLogger(__name__)


class ClimateRiskAPIView(APIView):
    """GET /api/mls/climate-risk/?lat=&lng=

    Returns heat / precipitation / flood projections for the coordinate.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        try:
            lat = float(request.query_params.get("lat", ""))
            lng = float(request.query_params.get("lng") or request.query_params.get("lon") or "")
        except (TypeError, ValueError):
            return Response({"error": "lat and lng are required numbers."}, status=400)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return Response({"error": "lat and lng are out of range."}, status=400)

        try:
            payload = build_climate_risk(latitude=lat, longitude=lng)
        except Exception:
            logger.exception("climate-risk failed lat=%s lng=%s", lat, lng)
            return Response(
                {"error": "Climate projections are temporarily unavailable."},
                status=502,
            )
        return Response(payload)
