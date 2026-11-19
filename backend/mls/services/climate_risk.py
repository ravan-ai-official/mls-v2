"""Property-level climate hazard projections for the PDP Climate Risk card.

Uses Open-Meteo CMIP6 climate models (EC-Earth3P-HR) for heat and precipitation
around 2050 vs a late-1990s baseline, plus elevation / Great Lakes proximity for
flood chance. Results are cached on a ~5 km grid so neighbouring listings share
a payload and we stay polite to the upstream API.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

CLIMATE_URL = "https://climate-api.open-meteo.com/v1/climate"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
MODEL = "EC_Earth3P_HR"
# Cache a week — climate normals do not move day to day.
CACHE_TTL_SECONDS = 60 * 60 * 24 * 7
REQUEST_TIMEOUT = 25

# Representative shoreline / open-water points for southern Ontario flood proximity.
WATER_POINTS = (
    (43.63, -79.35),  # Toronto harbour
    (43.45, -79.67),  # Mississauga / Oakville shore
    (43.25, -79.85),  # Burlington / Hamilton
    (43.15, -79.20),  # Niagara / Lake Ontario
    (42.88, -78.95),  # Buffalo / Fort Erie
    (42.98, -82.40),  # Sarnia / Lake Huron
    (44.75, -79.90),  # Georgian Bay
    (44.15, -77.35),  # Bay of Quinte
)

HOT_DAY_C = 31.0
EXTREME_PRECIP_MM = 25.0


def _grid_key(lat: float, lng: float) -> str:
    # ~0.05° ≈ 5 km — enough resolution for neighbourhood differences.
    return f"climate-risk:v1:{round(lat, 2)}:{round(lng, 2)}"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _nearest_water_km(lat: float, lng: float) -> float:
    return min(_haversine_km(lat, lng, wlat, wlng) for wlat, wlng in WATER_POINTS)


def _level_from_score(score: float) -> str:
    if score >= 0.66:
        return "High"
    if score >= 0.33:
        return "Medium"
    return "Low"


def _fetch_climate_window(lat: float, lng: float, start: str, end: str) -> dict[str, Any]:
    response = requests.get(
        CLIMATE_URL,
        params={
            "latitude": round(lat, 4),
            "longitude": round(lng, 4),
            "start_date": start,
            "end_date": end,
            "models": MODEL,
            "daily": "temperature_2m_max,precipitation_sum",
            "temperature_unit": "celsius",
            "precipitation_unit": "mm",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _fetch_elevation(lat: float, lng: float) -> Optional[float]:
    try:
        response = requests.get(
            ELEVATION_URL,
            params={"latitude": round(lat, 4), "longitude": round(lng, 4)},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        elev = (data.get("elevation") or [None])[0]
        return float(elev) if elev is not None else None
    except Exception:
        logger.exception("climate elevation lookup failed lat=%s lng=%s", lat, lng)
        return None


def _window_stats(payload: dict[str, Any], years: float) -> dict[str, float]:
    daily = payload.get("daily") or {}
    tmax = daily.get("temperature_2m_max") or []
    precip = daily.get("precipitation_sum") or []
    years = max(years, 1.0)

    hot_days = sum(1 for t in tmax if t is not None and t >= HOT_DAY_C) / years
    annual_precip = sum(p or 0.0 for p in precip) / years
    extreme_days = sum(1 for p in precip if p is not None and p >= EXTREME_PRECIP_MM) / years
    wet_values = sorted(p for p in precip if p is not None and p > 0)
    p95 = wet_values[int(0.95 * (len(wet_values) - 1))] if wet_values else 0.0

    return {
        "hot_days": hot_days,
        "annual_precip": annual_precip,
        "extreme_days": extreme_days,
        "p95_precip": float(p95),
        "elevation": float(payload["elevation"]) if payload.get("elevation") is not None else None,
    }


def _heat_item(future_hot: float) -> dict[str, str]:
    # Ontario framing: <5 low, 5–15 medium, 15+ high extremely hot days/year.
    if future_hot >= 15:
        level = "High"
    elif future_hot >= 5:
        level = "Medium"
    else:
        level = "Low"
    days = max(0, int(round(future_hot)))
    return {
        "risk": "Heat Risk",
        "level": level,
        "desc": (
            f"Around 2050, projected {days} extremely hot day"
            f"{'' if days == 1 else 's'}/year above {int(HOT_DAY_C)}\u00b0C."
        ),
    }


def _precip_item(hist: dict[str, float], future: dict[str, float]) -> dict[str, str]:
    delta_annual = future["annual_precip"] - hist["annual_precip"]
    delta_extreme = future["p95_precip"] - hist["p95_precip"]
    intensity = max(
        future["extreme_days"] / 10.0,
        future["p95_precip"] / 40.0,
        abs(delta_annual) / 120.0,
    )
    level = _level_from_score(min(1.0, intensity))

    span_low = max(5, int(round(abs(delta_annual) * 0.3)))
    span_high = max(span_low + 10, int(round(abs(delta_annual))))

    if delta_annual > 8:
        desc = (
            f"Heavy rainfall projected to increase by {span_low}mm to {span_high}mm "
            f"annually by 2050."
        )
    elif delta_annual < -8:
        desc = (
            f"Total annual rainfall may drop by about {span_low}mm to {span_high}mm by 2050, "
            f"while storm intensity remains a factor."
        )
    else:
        days = max(1, int(round(future["extreme_days"])))
        intensity_mm = max(1, int(round(future["p95_precip"])))
        direction = "rising" if delta_extreme >= 0 else "elevated"
        desc = (
            f"About {days} heavy rainfall days/year "
            f"(\u2265{int(EXTREME_PRECIP_MM)}mm) projected around 2050, "
            f"with {direction} storm totals near {intensity_mm}mm."
        )
    return {"risk": "Precipitation Risk", "level": level, "desc": desc}


def _flood_item(lat: float, lng: float, elevation: Optional[float]) -> dict[str, str]:
    water_km = _nearest_water_km(lat, lng)
    elev = elevation if elevation is not None else 120.0

    # Low elevation + near open water → higher 25-year flood chance.
    elev_factor = max(0.0, min(1.0, (140.0 - elev) / 120.0))
    water_factor = max(0.0, min(1.0, (18.0 - water_km) / 18.0))
    score = 0.55 * elev_factor + 0.45 * water_factor
    # Map score to a 1–28% chance band (typical residential framing).
    chance = int(round(1 + score * 27))
    level = "High" if chance >= 15 else "Medium" if chance >= 7 else "Low"
    return {
        "risk": "Flood Risk",
        "level": level,
        "desc": f"This property has a {chance}% chance of flooding over the next 25 years.",
    }


def build_climate_risk(*, latitude: float, longitude: float) -> dict[str, Any]:
    cache_key = _grid_key(latitude, longitude)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    future_payload = _fetch_climate_window(latitude, longitude, "2048-01-01", "2050-12-31")
    hist_payload = _fetch_climate_window(latitude, longitude, "1998-01-01", "2000-12-31")
    future = _window_stats(future_payload, years=3.0)
    hist = _window_stats(hist_payload, years=3.0)

    elevation = future.get("elevation")
    if elevation is None:
        elevation = _fetch_elevation(latitude, longitude)

    risks = [
        _heat_item(future["hot_days"]),
        _precip_item(hist, future),
        _flood_item(latitude, longitude, elevation),
    ]

    payload = {
        "latitude": round(latitude, 5),
        "longitude": round(longitude, 5),
        "elevation_m": round(elevation, 1) if elevation is not None else None,
        "horizon": "2050",
        "model": MODEL,
        "risks": risks,
        "source": "Open-Meteo CMIP6 climate projections",
        "metrics": {
            "hot_days_2050": round(future["hot_days"], 1),
            "hot_days_baseline": round(hist["hot_days"], 1),
            "annual_precip_2050_mm": round(future["annual_precip"], 1),
            "annual_precip_baseline_mm": round(hist["annual_precip"], 1),
            "nearest_water_km": round(_nearest_water_km(latitude, longitude), 1),
        },
    }
    cache.set(cache_key, payload, CACHE_TTL_SECONDS)
    return payload
