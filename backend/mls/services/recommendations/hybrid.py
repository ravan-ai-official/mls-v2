from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from django.db.models import Count, Q
from django.utils import timezone

from mls.models import ListingViewEvent, Property, UserFavorite, UserHistory, UserPropertyInteraction, UserToured
from mls.services.valuation.comps import haversine_km


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _is_rental(property: Property) -> bool:
    return bool(property.lease_amount or property.total_actual_rent)


def _comparison_price(property: Property):
    """Use monthly/lease price for rentals and list price for sale listings."""
    if _is_rental(property):
        return _to_float(property.lease_amount or property.total_actual_rent)
    return _to_float(property.list_price)


def _living_area(property: Property) -> Optional[float]:
    for key in ("living_area", "living_area_minimum", "living_area_maximum"):
        value = _to_float(getattr(property, key, None))
        if value and value > 0:
            return value
    return None


def _city_stem(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.split("(")[0].strip().lower()


def _home_type_key(property: Property) -> Optional[str]:
    """Canonical structure bucket so 'Single Family' condos are not peers of houses."""
    structure = (property.structure_type or "").strip().lower()
    attached = bool(property.property_attached_yn)
    if structure == "apartment" or "apartment" in structure:
        return "condo"
    if structure.startswith("row") or "townhouse" in structure or "townhome" in structure:
        return "townhouse"
    if structure == "house":
        return "semi" if attached else "detached"

    sub = (property.property_sub_type or "").strip().lower()
    if not sub:
        return None
    if any(token in sub for token in ("condo", "co-op", "coop", "apartment", "flat")):
        return "condo"
    if any(token in sub for token in ("town", "att/row", "row/", "freehold town")):
        return "townhouse"
    if "semi" in sub:
        return "semi"
    if any(token in sub for token in ("detached", "single family", "freehold")):
        return "detached"
    return None


def _bbox(lat: float, lng: float, radius_km: float) -> dict[str, float]:
    dlat = radius_km / 111.0
    dlng = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-6)
    return {
        "lat_min": lat - dlat,
        "lat_max": lat + dlat,
        "lng_min": lng - dlng,
        "lng_max": lng + dlng,
    }


@dataclass
class _Weights:
    # Content dominates: clients care that similar homes look like the subject.
    content: float = float(os.environ.get("REC_WEIGHT_CONTENT", "0.70"))
    personal: float = float(os.environ.get("REC_WEIGHT_PERSONAL", "0.18"))
    collaborative: float = float(os.environ.get("REC_WEIGHT_COLLAB", "0.07"))
    freshness: float = float(os.environ.get("REC_WEIGHT_FRESHNESS", "0.05"))


def _score_content(target: Property, candidate: Property) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    tlat, tlng = _to_float(target.latitude), _to_float(target.longitude)
    clat, clng = _to_float(candidate.latitude), _to_float(candidate.longitude)
    if None not in (tlat, tlng, clat, clng):
        distance = haversine_km(tlat, tlng, clat, clng)
        if distance <= 1.0:
            score += 0.30
            reasons.append("very_nearby")
        elif distance <= 3.0:
            score += 0.24
            reasons.append("nearby")
        elif distance <= 6.0:
            score += 0.16
            reasons.append("same_area")
        elif distance <= 12.0:
            score += 0.08
        else:
            score -= 0.12
    else:
        t_city, c_city = _city_stem(target.city), _city_stem(candidate.city)
        if t_city and c_city and t_city == c_city:
            score += 0.18
            reasons.append("same_city")
        elif target.city_region and candidate.city_region and target.city_region.lower() == candidate.city_region.lower():
            score += 0.10
            reasons.append("same_region")

    t_hood = (target.subdivision_name or target.city_region or "").strip().lower()
    c_hood = (candidate.subdivision_name or candidate.city_region or "").strip().lower()
    if t_hood and c_hood and t_hood == c_hood:
        score += 0.10
        reasons.append("same_neighbourhood")

    t_type, c_type = _home_type_key(target), _home_type_key(candidate)
    if t_type and c_type:
        if t_type == c_type:
            score += 0.28
            reasons.append("same_home_type")
        else:
            # Mixing condo with detached is the main client complaint.
            score -= 0.35
    elif (
        target.property_sub_type
        and candidate.property_sub_type
        and target.property_sub_type.lower() == candidate.property_sub_type.lower()
    ):
        score += 0.18
        reasons.append("same_type")

    target_price = _comparison_price(target)
    candidate_price = _comparison_price(candidate)
    if target_price and candidate_price:
        delta = abs(candidate_price - target_price) / max(target_price, 1)
        if delta <= 0.08:
            score += 0.22
            reasons.append("similar_rent" if _is_rental(target) else "similar_price")
        elif delta <= 0.18:
            score += 0.14
            reasons.append("close_rent" if _is_rental(target) else "close_price")
        elif delta <= 0.30:
            score += 0.07
        elif delta > 0.55:
            score -= 0.18

    target_beds = _to_int(target.bedrooms_total)
    candidate_beds = _to_int(candidate.bedrooms_total)
    if target_beds is not None and candidate_beds is not None:
        gap = abs(target_beds - candidate_beds)
        if gap == 0:
            score += 0.12
            reasons.append("same_beds")
        elif gap == 1:
            score += 0.05
        else:
            score -= 0.10

    target_baths = _to_int(target.bathrooms_total_integer)
    candidate_baths = _to_int(candidate.bathrooms_total_integer)
    if target_baths is not None and candidate_baths is not None:
        gap = abs(target_baths - candidate_baths)
        if gap == 0:
            score += 0.07
            reasons.append("same_baths")
        elif gap == 1:
            score += 0.03
        else:
            score -= 0.04

    t_sqft, c_sqft = _living_area(target), _living_area(candidate)
    if t_sqft and c_sqft:
        sqft_delta = abs(c_sqft - t_sqft) / max(t_sqft, 1)
        if sqft_delta <= 0.10:
            score += 0.12
            reasons.append("similar_size")
        elif sqft_delta <= 0.20:
            score += 0.06
        elif sqft_delta > 0.45:
            score -= 0.10

    return max(0.0, min(score, 1.0)), reasons


def _score_freshness(candidate: Property) -> float:
    ts = candidate.modification_timestamp
    if ts is None:
        return 0.0
    age_days = (timezone.now() - ts).days
    if age_days <= 3:
        return 1.0
    if age_days <= 7:
        return 0.7
    if age_days <= 14:
        return 0.45
    if age_days <= 30:
        return 0.2
    return 0.0


def _get_user_listing_keys(user) -> set[str]:
    if not user or not user.is_authenticated:
        return set()
    keys = set(UserHistory.objects.filter(user=user).values_list("property_key", flat=True)[:120])
    keys.update(UserFavorite.objects.filter(user=user).values_list("property_key", flat=True)[:120])
    keys.update(UserToured.objects.filter(user=user).values_list("property_key", flat=True)[:120])
    keys.update(
        UserPropertyInteraction.objects.filter(user=user).values_list("listing_key", flat=True)[:200]
    )
    return {str(k) for k in keys if k}


def _get_collaborative_keys(seed_keys: Iterable[str], limit: int = 60) -> list[str]:
    seed_keys = [str(k) for k in seed_keys if k]
    if not seed_keys:
        return []
    recent = timezone.now() - timedelta(days=45)
    sessions = list(
        ListingViewEvent.objects.filter(listing_key__in=seed_keys, created_at__gte=recent)
        .values_list("session_key", flat=True)
        .distinct()[:1500]
    )
    if not sessions:
        return []
    rows = (
        ListingViewEvent.objects.filter(session_key__in=sessions, created_at__gte=recent)
        .exclude(listing_key__in=seed_keys)
        .values("listing_key")
        .annotate(c=Count("id"))
        .order_by("-c")[:limit]
    )
    return [str(row["listing_key"]) for row in rows if row.get("listing_key")]


def _diversify(sorted_rows: list[dict], per_city_cap: int = 4) -> list[dict]:
    kept: list[dict] = []
    city_count = defaultdict(int)
    for row in sorted_rows:
        city = (row["property"].city or "").lower()
        if city and city_count[city] >= per_city_cap:
            continue
        kept.append(row)
        if city:
            city_count[city] += 1
    return kept


def build_recommendation_payload(
    *,
    target_property: Property,
    user,
    session_key: str,
    limit_per_section: int = 6,
) -> dict:
    weights = _Weights()
    enable_personal = os.environ.get("REC_ENABLE_PERSONAL", "1") != "0"
    enable_collab = os.environ.get("REC_ENABLE_COLLAB", "1") != "0"
    user_keys = _get_user_listing_keys(user)
    seed_keys = user_keys or ({target_property.listing_key} if target_property.listing_key else set())
    collaborative_keys = _get_collaborative_keys(seed_keys) if enable_collab else []
    collaborative_set = set(collaborative_keys)

    candidates_qs = (
        Property.objects.filter(standard_status__iexact="Active")
        .exclude(listing_key=target_property.listing_key)
        .exclude(listing_key__isnull=True)
    )
    if _is_rental(target_property):
        candidates_qs = candidates_qs.filter(
            Q(lease_amount__isnull=False) | Q(total_actual_rent__isnull=False)
        )
    else:
        candidates_qs = candidates_qs.filter(
            lease_amount__isnull=True,
            total_actual_rent__isnull=True,
        )

    broad_candidates_qs = candidates_qs
    tlat = _to_float(target_property.latitude)
    tlng = _to_float(target_property.longitude)
    if tlat is not None and tlng is not None:
        box = _bbox(tlat, tlng, 12.0)
        candidates_qs = candidates_qs.filter(
            latitude__gte=box["lat_min"],
            latitude__lte=box["lat_max"],
            longitude__gte=box["lng_min"],
            longitude__lte=box["lng_max"],
        )
    elif target_property.city:
        city = target_property.city.split("(")[0].strip()
        candidates_qs = candidates_qs.filter(
            Q(city__istartswith=city) | Q(city_region__iexact=target_property.city_region or "")
        )

    beds = _to_int(target_property.bedrooms_total)
    if beds is not None:
        candidates_qs = candidates_qs.filter(
            bedrooms_total__gte=max(0, beds - 1),
            bedrooms_total__lte=beds + 1,
        )

    base_price = _comparison_price(target_property)
    if base_price:
        lo, hi = base_price * 0.60, base_price * 1.50
        if _is_rental(target_property):
            candidates_qs = candidates_qs.filter(
                Q(lease_amount__gte=lo, lease_amount__lte=hi)
                | Q(total_actual_rent__gte=lo, total_actual_rent__lte=hi)
            )
        else:
            candidates_qs = candidates_qs.filter(list_price__gte=lo, list_price__lte=hi)

    home_type = _home_type_key(target_property)
    if home_type and target_property.structure_type:
        # Prefer same structure when the feed has it; subtype-only subjects skip this.
        structure = (target_property.structure_type or "").strip()
        if structure:
            candidates_qs = candidates_qs.filter(structure_type__iexact=structure)

    candidates = list(candidates_qs.order_by("-modification_timestamp")[:280])

    # Sparse markets should still have useful suggestions. Relax city/price only
    # after preserving transaction type, and never mix rentals with sales.
    if len(candidates) < max(limit_per_section * 2, 8):
        existing_keys = [candidate.listing_key for candidate in candidates]
        relaxed = broad_candidates_qs.exclude(listing_key__in=existing_keys)
        if tlat is not None and tlng is not None:
            box = _bbox(tlat, tlng, 25.0)
            relaxed = relaxed.filter(
                latitude__gte=box["lat_min"],
                latitude__lte=box["lat_max"],
                longitude__gte=box["lng_min"],
                longitude__lte=box["lng_max"],
            )
        elif target_property.city:
            city = target_property.city.split("(")[0].strip()
            relaxed = relaxed.filter(city__istartswith=city)
        if base_price:
            lo, hi = base_price * 0.45, base_price * 1.80
            if _is_rental(target_property):
                relaxed = relaxed.filter(
                    Q(lease_amount__gte=lo, lease_amount__lte=hi)
                    | Q(total_actual_rent__gte=lo, total_actual_rent__lte=hi)
                )
            else:
                relaxed = relaxed.filter(list_price__gte=lo, list_price__lte=hi)
        candidates.extend(list(relaxed.order_by("-modification_timestamp")[: 280 - len(candidates)]))

    ranked: list[dict] = []
    for candidate in candidates:
        content_score, reasons = _score_content(target_property, candidate)
        personal_score = 1.0 if enable_personal and candidate.listing_key in user_keys else 0.0
        collab_score = 1.0 if enable_collab and candidate.listing_key in collaborative_set else 0.0
        fresh_score = _score_freshness(candidate)
        total = (
            weights.content * content_score
            + weights.personal * personal_score
            + weights.collaborative * collab_score
            + weights.freshness * fresh_score
        )
        why = list(reasons)
        if personal_score > 0:
            why.append("matches_your_history")
        if collab_score > 0:
            why.append("also_viewed")
        ranked.append(
            {
                "property": candidate,
                "score": round(total, 4),
                "content_score": round(content_score, 4),
                "personal_score": round(personal_score, 4),
                "collab_score": round(collab_score, 4),
                "freshness_score": round(fresh_score, 4),
                "why": why[:3],
            }
        )

    ranked.sort(key=lambda row: row["score"], reverse=True)
    ranked = _diversify(ranked)

    def _section(rows: list[dict], predicate):
        out = []
        for row in rows:
            if predicate(row):
                out.append(row)
            if len(out) >= limit_per_section:
                break
        return out

    for_this_home = _section(ranked, lambda row: row["content_score"] >= 0.38)
    based_on_history = (
        _section(
            ranked,
            lambda row: row["personal_score"] > 0 or "matches_your_history" in row["why"],
        )
        if enable_personal
        else []
    )
    people_also_viewed = (
        _section(
            ranked,
            lambda row: row["collab_score"] > 0 or "also_viewed" in row["why"],
        )
        if enable_collab
        else []
    )

    seen_keys = {row["property"].listing_key for row in for_this_home + based_on_history + people_also_viewed}
    fallback_rows = [row for row in ranked if row["property"].listing_key not in seen_keys][:limit_per_section]

    confidence = {
        "for_this_home": "high" if len(for_this_home) >= 3 else "medium" if for_this_home else "low",
        "based_on_your_history": "high" if len(based_on_history) >= 3 else "medium" if based_on_history else "low",
        "people_also_viewed": "high" if len(people_also_viewed) >= 3 else "medium" if people_also_viewed else "low",
    }

    return {
        "sections": {
            "for_this_home": for_this_home,
            "based_on_your_history": based_on_history,
            "people_also_viewed": people_also_viewed,
        },
        "fallback": {
            "applied": len(for_this_home) == 0 or len(based_on_history) == 0,
            "rows": fallback_rows,
        },
        "metadata": {
            "weights": {
                "content": weights.content,
                "personal": weights.personal,
                "collaborative": weights.collaborative,
                "freshness": weights.freshness,
            },
            "candidate_count": len(candidates),
            "feature_flags": {
                "enable_personal": enable_personal,
                "enable_collaborative": enable_collab,
            },
            "section_sizes": {
                "for_this_home": len(for_this_home),
                "based_on_your_history": len(based_on_history),
                "people_also_viewed": len(people_also_viewed),
                "fallback": len(fallback_rows),
            },
            "session_key": session_key or "",
            "generated_at": timezone.now(),
            "confidence": confidence,
        },
    }
