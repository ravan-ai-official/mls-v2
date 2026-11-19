"""Strong sold / for-sale / rent comps for the property detail Comparables tab.

Uses progressive distance + type + beds + price + size matching so the tab
shows peers of the subject, not merely the newest homes in the same city string.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.db.models import Q
from django.utils import timezone

from mls.models import Property
from mls.services.valuation.comps import haversine_km

from .hybrid import (
    _comparison_price,
    _home_type_key,
    _living_area,
    _to_float,
    _to_int,
)


PASSES = (
    # (radius_km, beds_tol, price_tol, sqft_tol, require_type, min_count)
    (2.0, 0, 0.15, 0.12, True, 4),
    (5.0, 1, 0.25, 0.18, True, 4),
    (10.0, 1, 0.40, 0.28, False, 3),
    (20.0, 2, 0.55, 0.40, False, 2),
)


def _bbox(lat: float, lng: float, radius_km: float) -> dict[str, float]:
    # ~111 km per degree latitude; longitude shrinks with cos(lat).
    import math

    dlat = radius_km / 111.0
    dlng = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-6)
    return {
        "lat_min": lat - dlat,
        "lat_max": lat + dlat,
        "lng_min": lng - dlng,
        "lng_max": lng + dlng,
    }


def _peer_price(property: Property, *, kind: str) -> float | None:
    if kind == "sold":
        return _to_float(property.close_price) or _comparison_price(property)
    if kind == "rent":
        return _to_float(property.lease_amount or property.total_actual_rent)
    return _comparison_price(property)


def _subject_price(subject: Property, *, kind: str) -> float | None:
    """Anchor the subject against the bucket's price field (list / lease / close)."""
    if kind == "rent":
        return _to_float(subject.lease_amount or subject.total_actual_rent) or _comparison_price(subject)
    # Sale + sold comps are priced against the subject's asking (or prior close).
    return _comparison_price(subject) or _to_float(subject.close_price)


def _rank_key(subject: Property, candidate: Property, *, kind: str) -> tuple:
    slat, slng = _to_float(subject.latitude), _to_float(subject.longitude)
    clat, clng = _to_float(candidate.latitude), _to_float(candidate.longitude)
    distance = 99.0
    if None not in (slat, slng, clat, clng):
        distance = haversine_km(slat, slng, clat, clng)

    sub_type = _home_type_key(subject)
    cand_type = _home_type_key(candidate)
    type_pen = 0.0 if sub_type and cand_type and sub_type == cand_type else 1.0

    beds = _to_int(subject.bedrooms_total)
    cbeds = _to_int(candidate.bedrooms_total)
    bed_pen = abs((beds or 0) - (cbeds or 0)) if beds is not None and cbeds is not None else 1

    sub_price = _subject_price(subject, kind=kind)
    cand_price = _peer_price(candidate, kind=kind)
    price_pen = 1.0
    if sub_price and cand_price:
        price_pen = abs(cand_price - sub_price) / max(sub_price, 1)

    sub_sqft = _living_area(subject)
    cand_sqft = _living_area(candidate)
    sqft_pen = 1.0
    if sub_sqft and cand_sqft:
        sqft_pen = abs(cand_sqft - sub_sqft) / max(sub_sqft, 1)

    return (distance, type_pen, bed_pen, price_pen, sqft_pen)


def _passes(
    subject: Property,
    candidate: Property,
    *,
    kind: str,
    radius_km: float,
    beds_tol: int,
    price_tol: float,
    sqft_tol: float,
    require_type: bool,
) -> bool:
    slat, slng = _to_float(subject.latitude), _to_float(subject.longitude)
    clat, clng = _to_float(candidate.latitude), _to_float(candidate.longitude)
    if None not in (slat, slng, clat, clng):
        if haversine_km(slat, slng, clat, clng) > radius_km:
            return False
    elif subject.city and candidate.city:
        if subject.city.split("(")[0].strip().lower() != candidate.city.split("(")[0].strip().lower():
            # Without coords, stay in the same municipality string.
            if radius_km <= 10:
                return False

    sub_type = _home_type_key(subject)
    cand_type = _home_type_key(candidate)
    if require_type and sub_type and cand_type and sub_type != cand_type:
        return False

    beds = _to_int(subject.bedrooms_total)
    cbeds = _to_int(candidate.bedrooms_total)
    if beds is not None and cbeds is not None and abs(beds - cbeds) > beds_tol:
        return False

    sub_price = _subject_price(subject, kind=kind)
    cand_price = _peer_price(candidate, kind=kind)
    if sub_price and cand_price:
        if abs(cand_price - sub_price) / max(sub_price, 1) > price_tol:
            return False

    sub_sqft = _living_area(subject)
    cand_sqft = _living_area(candidate)
    if sub_sqft and cand_sqft:
        if abs(cand_sqft - sub_sqft) / max(sub_sqft, 1) > sqft_tol:
            return False

    return True


def _base_qs(subject: Property, kind: str):
    qs = Property.objects.exclude(listing_key=subject.listing_key).exclude(listing_key__isnull=True)
    if kind == "sold":
        return qs.filter(
            Q(standard_status__iexact="Closed") | Q(standard_status__iexact="Sold"),
            close_price__gt=0,
            close_date__gte=(timezone.now().date() - timedelta(days=730)),
        )
    if kind == "rent":
        return qs.filter(standard_status__iexact="Active").filter(
            Q(lease_amount__gt=0) | Q(total_actual_rent__gt=0)
        )
    # sale
    return qs.filter(
        standard_status__iexact="Active",
        list_price__gt=0,
        lease_amount__isnull=True,
        total_actual_rent__isnull=True,
    )


def _select_bucket(subject: Property, kind: str, limit: int) -> list[Property]:
    qs = _base_qs(subject, kind)
    lat, lng = _to_float(subject.latitude), _to_float(subject.longitude)
    if lat is not None and lng is not None:
        box = _bbox(lat, lng, 25.0)
        qs = qs.filter(
            latitude__gte=box["lat_min"],
            latitude__lte=box["lat_max"],
            longitude__gte=box["lng_min"],
            longitude__lte=box["lng_max"],
        )
    elif subject.city:
        city = subject.city.split("(")[0].strip()
        qs = qs.filter(city__istartswith=city)

    beds = _to_int(subject.bedrooms_total)
    if beds is not None:
        qs = qs.filter(bedrooms_total__gte=max(0, beds - 2), bedrooms_total__lte=beds + 2)

    order = "-close_date" if kind == "sold" else "-modification_timestamp"
    pool = list(qs.order_by(order)[:400])

    picked: list[Property] = []
    seen: set[str] = set()
    for radius_km, beds_tol, price_tol, sqft_tol, require_type, min_count in PASSES:
        for candidate in pool:
            key = candidate.listing_key
            if not key or key in seen:
                continue
            if not _passes(
                subject,
                candidate,
                kind=kind,
                radius_km=radius_km,
                beds_tol=beds_tol,
                price_tol=price_tol,
                sqft_tol=sqft_tol,
                require_type=require_type,
            ):
                continue
            seen.add(key)
            picked.append(candidate)
            if len(picked) >= limit:
                return sorted(picked, key=lambda row: _rank_key(subject, row, kind=kind))[:limit]
        if len(picked) >= min_count:
            break

    if len(picked) < limit:
        for candidate in pool:
            key = candidate.listing_key
            if not key or key in seen:
                continue
            seen.add(key)
            picked.append(candidate)
            if len(picked) >= limit:
                break

    return sorted(picked, key=lambda row: _rank_key(subject, row, kind=kind))[:limit]


def build_comparables_payload(*, target_property: Property, limit: int = 4) -> dict[str, list[Property]]:
    return {
        "sale": _select_bucket(target_property, "sale", limit),
        "sold": _select_bucket(target_property, "sold", limit),
        "rent": _select_bucket(target_property, "rent", limit),
    }
