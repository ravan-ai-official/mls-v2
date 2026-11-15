"""
Pull recent Closed / Expired / Cancelled / Withdrawn listings from AMPRE
into the local Property table so Sold and De-listed tabs have rows.

CREA DDF (realtor.ca) is Active-only, so pulling more Active listings never
fills those tabs. AMPRE carries the off-market history the tabs need.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from homepage.services.ampre import RESIDENTIAL_CLAUSE, city_clause
from mls.models import Property
from mls.services.ampre_client import AmpreClientError, fetch_property_page
from mls.services.ampre_media import (
    is_trreb_property,
    refresh_property_media_from_ampre,
)
from mls.views_market import GTA_CITIES


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
    "Latitude",
    "Longitude",
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
        return int(value)
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


def map_ampre_inactive(row: dict) -> dict:
    """Reuse the shared AMPRE field map so closed/terminated rows stay rich."""
    from mls.services.listing_refresh import map_ampre_row

    mapped = map_ampre_row(row)
    status = (mapped.get("standard_status") or "Closed").strip() or "Closed"
    mapped["standard_status"] = status[:50]
    mapped["category_type"] = Property.DDF
    return mapped


class Command(BaseCommand):
    help = (
        "Upsert recent AMPRE Closed/Expired/Cancelled/Withdrawn listings so "
        "Sold and De-listed search tabs are populated."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=90,
            help="Look back this many days (default: 90).",
        )
        parser.add_argument(
            "--max-rows-per-city",
            type=int,
            default=800,
            help="Max AMPRE rows per city per status family (default: 800).",
        )

    def handle(self, *args, **options):
        days = max(1, int(options["days"]))
        max_rows = max(50, int(options["max_rows_per_city"]))
        today = timezone.now().date()
        start = (today - timedelta(days=days)).isoformat()
        end = (today + timedelta(days=1)).isoformat()

        created = 0
        updated = 0
        fetched = 0
        media_filled = 0

        for city in GTA_CITIES:
            city_filter = city_clause([city])

            queries = [
                (
                    "Closed",
                    f"({city_filter}) and StandardStatus eq 'Closed' "
                    f"and TransactionType eq 'For Sale' and {RESIDENTIAL_CLAUSE} "
                    f"and CloseDate ge {start} and CloseDate le {end}",
                    "CloseDate desc",
                ),
                (
                    "De-listed",
                    f"({city_filter}) and ("
                    "StandardStatus eq 'Expired' or StandardStatus eq 'Canceled' "
                    "or StandardStatus eq 'Cancelled' or StandardStatus eq 'Withdrawn' "
                    "or StandardStatus eq 'Terminated'"
                    f") and {RESIDENTIAL_CLAUSE} "
                    f"and ModificationTimestamp ge {start}T00:00:00Z",
                    "ModificationTimestamp desc",
                ),
            ]

            for label, filter_expression, orderby in queries:
                self.stdout.write(f"{city} · {label}...")
                try:
                    rows = fetch_property_page(
                        filter_expression=filter_expression,
                        select_fields=SELECT_FIELDS,
                        orderby=orderby,
                        top=min(1000, max_rows),
                        max_rows=max_rows,
                    )
                except AmpreClientError as exc:
                    self.stderr.write(self.style.WARNING(f"  skipped: {exc}"))
                    continue

                fetched += len(rows)
                for row in rows:
                    ampre_key = str(row.get("ListingKey") or "").strip()
                    if not ampre_key:
                        continue
                    defaults = map_ampre_inactive(row)
                    existing = (
                        Property.objects.filter(
                            Q(listing_id=ampre_key) | Q(listing_key=ampre_key)
                        )
                        .order_by("id")
                        .first()
                    )
                    if existing:
                        for field, value in defaults.items():
                            if field == "category_type":
                                continue
                            setattr(existing, field, value)
                        existing.save()
                        updated += 1
                        prop = existing
                    else:
                        prop = Property.objects.create(
                            listing_key=ampre_key, **defaults
                        )
                        created += 1

                    # Closed sync historically wrote listing rows with no
                    # Media, so Sold / De-listed cards showed the house
                    # placeholder. Pull live AMPRE photos when the gallery
                    # is empty.
                    if (
                        is_trreb_property(prop)
                        and not prop.media.exists()
                    ):
                        try:
                            written = refresh_property_media_from_ampre(prop)
                        except AmpreClientError as exc:
                            self.stderr.write(
                                self.style.WARNING(
                                    f"  media skip {prop.listing_id or ampre_key}: {exc}"
                                )
                            )
                            written = 0
                        if written:
                            media_filled += 1

                self.stdout.write(f"  {len(rows):,} rows")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Fetched {fetched:,}. Created {created:,}, updated {updated:,}. "
                f"Filled media for {media_filled:,}."
            )
        )
