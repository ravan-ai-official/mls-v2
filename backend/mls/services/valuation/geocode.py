"""Nominatim geocode helper for valuation when a listing has no lat/lng."""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)


def geocode_address(query: str) -> tuple[Optional[float], Optional[float]]:
    """Return (lat, lng) for a free-text address, or (None, None) on failure."""
    cleaned = " ".join((query or "").split()).strip()
    if len(cleaned) < 5:
        return None, None

    cache_key = f"valuation-geocode:{cleaned.lower()[:200]}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        try:
            return float(cached["lat"]), float(cached["lng"])
        except (KeyError, TypeError, ValueError):
            pass

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": cleaned,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "ca,us",
            },
            headers={
                "User-Agent": os.environ.get(
                    "MAP_GEOCODER_USER_AGENT", "HomeAtlasValuation/1.0"
                ),
                "Accept-Language": "en",
            },
            timeout=8,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError, TypeError):
        logger.exception("Valuation geocode failed for %r", cleaned)
        return None, None

    if not isinstance(rows, list) or not rows:
        return None, None
    row = rows[0] if isinstance(rows[0], dict) else {}
    try:
        lat = float(row["lat"])
        lng = float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None

    cache.set(cache_key, {"lat": lat, "lng": lng}, 60 * 60 * 24)
    return lat, lng
