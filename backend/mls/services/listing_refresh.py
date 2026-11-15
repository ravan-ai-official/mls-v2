"""
On-demand refresh of a single listing from AMPRE.

Used when the rolling DDF cache is stale (Celery not running, or a listing
left the Active-only DDF feed without a status write). Soft-fails so detail
pages still render from the local row.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from mls.models import Property
from mls.services.ampre_client import AmpreClientError, fetch_property_page

logger = logging.getLogger(__name__)

# Active rows older than this are considered stale enough to re-check AMPRE.
STALE_ACTIVE_HOURS = 36

SELECT_FIELDS = [
    "ListingKey",
    "ListingId",
    "StandardStatus",
    "ListPrice",
    "ClosePrice",
    "CloseDate",
    "PreviousListPrice",
    "PriceChangeTimestamp",
    "UnparsedAddress",
    "City",
    "CityRegion",
    "StateOrProvince",
    "PostalCode",
    "PropertySubType",
    "StructureType",
    "PropertyAttachedYN",
    "BedroomsTotal",
    "BedroomsAboveGrade",
    "BedroomsBelowGrade",
    "BathroomsTotalInteger",
    "LivingArea",
    "LivingAreaMinimum",
    "LivingAreaMaximum",
    "AboveGradeFinishedArea",
    "BuildingAreaTotal",
    "YearBuilt",
    "ApproximateAge",
    "TaxAnnualAmount",
    "AssociationFee",
    "AssociationFeeFrequency",
    "AssociationFeeIncludes",
    "ParkingTotal",
    "ParkingFeatures",
    "BuildingFeatures",
    "CommonInterest",
    "PublicRemarks",
    "OriginalEntryTimestamp",
    "ModificationTimestamp",
    "TransactionType",
]


def _dec(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _dt(value):
    if not value:
        return None
    if isinstance(value, str):
        return parse_datetime(value.replace("Z", "+00:00")) or None
    return value


def _date(value):
    if not value:
        return None
    if isinstance(value, str):
        return parse_date(value[:10])
    return value


def _str_or_join(value, limit=2000):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value if item not in (None, ""))
    else:
        text = str(value)
    text = text.strip()
    return text[:limit] if text else None


def _year_from_age(raw):
    age = _int(raw)
    if age is None or age < 0 or age >= 300:
        return None
    return timezone.now().year - age


def map_ampre_row(row: dict) -> dict:
    living = _dec(row.get("LivingArea"))
    living_min = _dec(row.get("LivingAreaMinimum"))
    living_max = _dec(row.get("LivingAreaMaximum"))
    if living is None:
        living = living_min or living_max

    structure = row.get("StructureType")
    if isinstance(structure, list):
        structure = structure[0] if structure else None

    return {
        "listing_id": (_str_or_join(row.get("ListingId") or row.get("ListingKey"), 50)),
        "standard_status": (_str_or_join(row.get("StandardStatus"), 50) or "Active"),
        "list_price": _dec(row.get("ListPrice")),
        "close_price": _dec(row.get("ClosePrice")),
        "close_date": _date(row.get("CloseDate")),
        "previous_list_price": _dec(row.get("PreviousListPrice")),
        "price_change_timestamp": _dt(row.get("PriceChangeTimestamp")),
        "unparsed_address": _str_or_join(row.get("UnparsedAddress")),
        "city": _str_or_join(row.get("City")),
        "city_region": _str_or_join(row.get("CityRegion")),
        "state_or_province": _str_or_join(row.get("StateOrProvince"), 100) or "Ontario",
        "postal_code": _str_or_join(row.get("PostalCode"), 20),
        "property_sub_type": _str_or_join(row.get("PropertySubType"), 100),
        "structure_type": _str_or_join(structure, 100),
        "property_attached_yn": row.get("PropertyAttachedYN"),
        "bedrooms_total": _int(row.get("BedroomsTotal")),
        "bedrooms_above_grade": _int(row.get("BedroomsAboveGrade")),
        "bedrooms_below_grade": _int(row.get("BedroomsBelowGrade")),
        "bathrooms_total_integer": _int(row.get("BathroomsTotalInteger")),
        "living_area": living,
        "living_area_minimum": living_min,
        "living_area_maximum": living_max,
        "above_grade_finished_area": _dec(row.get("AboveGradeFinishedArea")),
        "building_area_total": _dec(row.get("BuildingAreaTotal")),
        "year_built": _int(row.get("YearBuilt")) or _year_from_age(row.get("ApproximateAge")),
        "tax_annual_amount": _dec(row.get("TaxAnnualAmount")),
        "association_fee": _dec(row.get("AssociationFee")),
        "association_fee_frequency": _str_or_join(row.get("AssociationFeeFrequency"), 50),
        "association_fee_includes": _str_or_join(row.get("AssociationFeeIncludes")),
        "parking_total": _int(row.get("ParkingTotal")),
        "parking_features": _str_or_join(row.get("ParkingFeatures")),
        "building_features": _str_or_join(row.get("BuildingFeatures")),
        "common_interest": _str_or_join(row.get("CommonInterest"), 100),
        "public_remarks": row.get("PublicRemarks") or "",
        "original_entry_timestamp": _dt(row.get("OriginalEntryTimestamp")),
        "modification_timestamp": _dt(row.get("ModificationTimestamp")) or timezone.now(),
        "status_change_timestamp": _dt(row.get("ModificationTimestamp")) or timezone.now(),
    }


def listing_is_stale_active(prop: Property, *, hours: int = STALE_ACTIVE_HOURS) -> bool:
    status = (prop.standard_status or "").strip().lower()
    if status != "active":
        return False
    mod = prop.modification_timestamp
    if mod is None:
        return True
    return timezone.now() - mod >= timedelta(hours=hours)


def refresh_listing_from_ampre(prop: Property) -> Property | None:
    """
    Re-fetch one listing from AMPRE by ListingId / ListingKey and apply updates.

    Returns the saved property, or None when AMPRE has no row / is unavailable.
    When AMPRE has no match for a stale Active row, mark it Expired so the
    site stops advertising it as For Sale.
    """
    keys = []
    for raw in (prop.listing_id, prop.listing_key):
        value = (raw or "").strip()
        if value and value not in keys:
            keys.append(value)
    if not keys:
        return None

    clauses = []
    for key in keys:
        safe = key.replace("'", "''")
        clauses.append(f"ListingId eq '{safe}'")
        clauses.append(f"ListingKey eq '{safe}'")
    filter_expression = " or ".join(clauses)

    try:
        rows = fetch_property_page(
            filter_expression=filter_expression,
            select_fields=SELECT_FIELDS,
            top=5,
            max_rows=5,
        )
    except AmpreClientError as exc:
        logger.warning("AMPRE refresh skipped for %s: %s", prop.listing_key, exc)
        return None

    if not rows:
        # Gone from AMPRE too — treat as off-market rather than forever-Active.
        if listing_is_stale_active(prop):
            prop.standard_status = "Expired"
            prop.status_change_timestamp = timezone.now()
            prop.save(
                update_fields=["standard_status", "status_change_timestamp"]
            )
            return prop
        return None

    row = rows[0]
    defaults = map_ampre_row(row)
    old_price = prop.list_price
    new_price = defaults.get("list_price")
    if (
        old_price is not None
        and new_price is not None
        and old_price != new_price
        and not defaults.get("previous_list_price")
    ):
        defaults["previous_list_price"] = old_price
        defaults["price_change_timestamp"] = (
            defaults.get("modification_timestamp") or timezone.now()
        )

    changed = []
    for field, value in defaults.items():
        if value is None:
            continue
        if getattr(prop, field, None) != value:
            setattr(prop, field, value)
            changed.append(field)
    if changed:
        prop.save(update_fields=changed)
    return prop


def maybe_refresh_stale_listing(prop: Property) -> Property:
    """Refresh when Active and stale; otherwise return the row unchanged."""
    if not listing_is_stale_active(prop):
        return prop
    return refresh_listing_from_ampre(prop) or prop
