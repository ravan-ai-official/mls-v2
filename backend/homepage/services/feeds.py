"""Feed builders behind the homepage endpoints.

Each function turns a data source (the local catalogue, AMPRE, or a derived
table) into the JSON a section renders, and caches it: the homepage is ISR on
the frontend, so these run a few times an hour, not per visitor.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from mls.models import Media, Property
from mls.services.ampre_client import AmpreClientError
from mls.views_market import GTA_CITIES

from . import ampre
from .common import base_city, to_float
from .deals import rank_price_drops
from .market_snapshot import build_snapshot
from .rent_estimates import CALC_BASIS, RentalComp, rank_candidates

logger = logging.getLogger(__name__)

INVESTOR_TTL = 60 * 60
DEALS_TTL = 30 * 60
SNAPSHOT_MAX_AGE = 6 * 60 * 60
INVESTOR_CANDIDATE_POOL = 600
# 6+ bedroom listings are mostly rooming houses and multiplexes whose few
# rental comps are whole-building rents; they swamp the ranking with yields
# a typical investor can't reproduce, so the feed sticks to 1–5 bedrooms.
INVESTOR_MAX_BEDROOMS = 5
# DDF / PropTX residential labels used in the live GTA catalogue. The old
# ("Single Family", "Multi-family") pair matched almost nothing, so the rail
# stayed empty even when rents and sale inventory were plentiful.
RESIDENTIAL_SUB_TYPES = (
    "Detached",
    "Semi-Detached",
    "Att/Row/Townhouse",
    "Condo Apartment",
    "Condo Townhouse",
    "Link",
    "Duplex",
    "Triplex",
    "Fourplex",
    "Multiplex",
    "Detached Condo",
    "Semi-Detached Condo",
    "Common Element Condo",
    "Single Family",
    "Multi-family",
)
# Monthly rents often land in list_price too; anything below this is not a sale.
MIN_SALE_PRICE = 150_000
# Asking-rent sanity band for comps (monthly).
RENT_COMP_RANGE = (500, 20_000)


class FeedUnavailable(RuntimeError):
    """Upstream (AMPRE) failed and there is no cached copy to fall back on."""


def _cached(key: str, ttl: int, build):
    value = cache.get(key)
    if value is None:
        value = build()
        cache.set(key, value, ttl)
    return value


def _gta_q() -> Q:
    q = Q()
    for city in GTA_CITIES:
        q |= Q(city__istartswith=city)
    return q


def _photos(listing_keys: Iterable[str]) -> dict[str, str]:
    """First preferred-or-lowest-order photo per listing, in one query."""
    photos: dict[str, str] = {}
    rows = (
        Media.objects.filter(property__listing_key__in=list(listing_keys))
        .order_by("property__listing_key", "-is_preferred", "order")
        .values_list("property__listing_key", "media_url")
    )
    for key, url in rows:
        photos.setdefault(key, url)
    return photos


def _local_by_mls(mls_numbers: Iterable[str]) -> dict[str, str]:
    """AMPRE ListingKey → local listing_key. TRREB listings in the DDF feed
    carry the TRREB number in ``listing_id``, which is how they're matched."""
    return dict(
        Property.objects.filter(listing_id__in=list(mls_numbers)).values_list("listing_id", "listing_key")
    )


def _ampre_primary_photos(mls_numbers: Iterable[str]) -> dict[str, str]:
    """Preferred/first AMPRE MediaURL per TRREB MLS / ListingKey.

    Price-drop deals come from AMPRE and often are not in the local DDF
    catalogue yet, so ``_photos`` has nothing to join. Hitting Media for the
    handful of ranked deals (typically ≤4) keeps the cards from rendering
    blank while the listing sync catches up.
    """
    from mls.services.ampre_media import fetch_ampre_photos_for_listing_id

    out: dict[str, str] = {}
    for mls in mls_numbers:
        key = (mls or "").strip()
        if not key or key in out:
            continue
        try:
            photos = fetch_ampre_photos_for_listing_id(key)
        except AmpreClientError as exc:
            logger.warning("AMPRE media miss for deal %s: %s", key, exc)
            continue
        if not photos:
            continue
        url = (photos[0].get("MediaURL") or "").strip()
        if url:
            out[key] = url
    return out


# --------------------------------------------------------------------------
# High return properties
# --------------------------------------------------------------------------


def _investor_result_row(r: dict[str, Any], photos: dict[str, str], *, pinned: bool) -> dict[str, Any]:
    return {
        "listing_key": r["listing_key"],
        "address": r["address"],
        "city": base_city(r["city"]),
        "bedrooms": r["bedrooms"],
        "bathrooms": r["bathrooms"],
        "list_price": to_float(r["price"]),
        "image_url": photos.get(r["listing_key"]),
        "estimated_monthly_rent": r["metrics"].estimated_monthly_rent,
        "gross_yield_pct": r["metrics"].gross_yield_pct,
        "cap_rate_pct": r["metrics"].cap_rate_pct,
        "comps_used": r["metrics"].comps_used,
        "source": "pinned" if pinned else "ranked",
    }


def investor_picks(limit: int = 3, city: str | None = None) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        from django.db.models import F
        from django.db.models.functions import Coalesce

        now = timezone.now()
        base = Property.objects.filter(standard_status__iexact="active").filter(_gta_q())
        # Rentals: lease_amount OR total_actual_rent (DDF often also fills list_price
        # with the monthly rent, so we must NOT require list_price IS NULL).
        rent_lo, rent_hi = RENT_COMP_RANGE
        rental_rows = (
            base.filter(Q(lease_amount__gt=0) | Q(total_actual_rent__gt=0))
            .annotate(rent_amount=Coalesce("lease_amount", "total_actual_rent"))
            .filter(rent_amount__gte=rent_lo, rent_amount__lte=rent_hi)
            .values_list("city", "bedrooms_total", "rent_amount")
        )
        comps = [
            RentalComp(city=c or "", bedrooms=b, monthly_rent=float(r))
            for c, b, r in rental_rows
            if r is not None
        ]

        pin_qs = (
            Property.objects.filter(is_high_return=True, standard_status__iexact="active")
            .filter(Q(high_return_until__isnull=True) | Q(high_return_until__gt=now))
            .order_by(F("high_return_order").asc(nulls_last=True), "-listing_key")
        )
        if city:
            pin_qs = pin_qs.filter(city__istartswith=city)
        pin_candidates = [
            {
                "listing_key": row["listing_key"],
                "address": row["unparsed_address"] or "",
                "city": row["city"] or "",
                "bedrooms": row["bedrooms_total"],
                "bathrooms": row["bathrooms_total_integer"],
                "price": row["list_price"],
                "annual_tax": row["tax_annual_amount"],
                "condo_fee": row["association_fee"],
                "condo_fee_frequency": row["association_fee_frequency"],
            }
            for row in pin_qs.values(
                "listing_key",
                "unparsed_address",
                "city",
                "bedrooms_total",
                "bathrooms_total_integer",
                "list_price",
                "tax_annual_amount",
                "association_fee",
                "association_fee_frequency",
            )[:limit]
        ]
        # Score pins for yield metrics but keep admin order (not yield rank).
        pinned_rows: list[dict[str, Any]] = []
        if pin_candidates:
            scored_pins = {
                row["listing_key"]: row
                for row in rank_candidates(pin_candidates, comps, limit=len(pin_candidates))
            }
            pinned_rows = [
                scored_pins[c["listing_key"]]
                for c in pin_candidates
                if c["listing_key"] in scored_pins
            ]
        pinned_keys = {r["listing_key"] for r in pinned_rows}

        fill_needed = max(0, limit - len(pinned_rows))
        ranked: list[dict[str, Any]] = []
        if fill_needed:
            # Sale homes only: exclude leases and monthly-rent-sized list prices.
            sales = (
                base.filter(
                    list_price__gte=MIN_SALE_PRICE,
                    bedrooms_total__gt=0,
                    bedrooms_total__lte=INVESTOR_MAX_BEDROOMS,
                    property_sub_type__in=RESIDENTIAL_SUB_TYPES,
                    lease_amount__isnull=True,
                    total_actual_rent__isnull=True,
                ).exclude(listing_key__in=pinned_keys)
            )
            if city:
                sales = sales.filter(city__istartswith=city)
            candidates = [
                {
                    "listing_key": row["listing_key"],
                    "address": row["unparsed_address"] or "",
                    "city": row["city"] or "",
                    "bedrooms": row["bedrooms_total"],
                    "bathrooms": row["bathrooms_total_integer"],
                    "price": row["list_price"],
                    "annual_tax": row["tax_annual_amount"],
                    "condo_fee": row["association_fee"],
                    "condo_fee_frequency": row["association_fee_frequency"],
                }
                for row in sales.order_by("-original_entry_timestamp").values(
                    "listing_key",
                    "unparsed_address",
                    "city",
                    "bedrooms_total",
                    "bathrooms_total_integer",
                    "list_price",
                    "tax_annual_amount",
                    "association_fee",
                    "association_fee_frequency",
                )[:INVESTOR_CANDIDATE_POOL]
            ]
            ranked = rank_candidates(candidates, comps, fill_needed)

        combined = pinned_rows + ranked
        photos = _photos(r["listing_key"] for r in combined)
        return {
            "results": [
                _investor_result_row(r, photos, pinned=i < len(pinned_rows))
                for i, r in enumerate(combined)
            ],
            "pinned_count": len(pinned_rows),
            "calc_basis": CALC_BASIS,
            "generated_at": timezone.now().isoformat(),
        }

    # v2 busts the empty cache left by the old subtype / rental-comp filters.
    return _cached(f"home:investor:v2:{limit}:{(city or '').lower()}", INVESTOR_TTL, build)


# --------------------------------------------------------------------------
# GTA market deals
# --------------------------------------------------------------------------


def market_deals(limit: int = 4) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        try:
            rows = ampre.fetch_price_drops()
        except AmpreClientError as exc:
            raise FeedUnavailable(str(exc)) from exc
        deals = rank_price_drops(rows, limit)
        local = _local_by_mls(d.mls_number for d in deals)
        photos = _photos(local.values())
        need_ampre = [
            d.mls_number
            for d in deals
            if not photos.get(local.get(d.mls_number) or "")
        ]
        ampre_photos = _ampre_primary_photos(need_ampre) if need_ampre else {}
        results = []
        for deal in deals:
            key = local.get(deal.mls_number)
            image = (photos.get(key) if key else None) or ampre_photos.get(deal.mls_number)
            results.append({**deal.as_dict(), "listing_key": key, "image_url": image})
        return {"results": results, "generated_at": timezone.now().isoformat()}

    return _cached(f"home:deals:{limit}", DEALS_TTL, build)


# --------------------------------------------------------------------------
# Sold below last purchase (table first; local/AMPRE fill when empty)
# --------------------------------------------------------------------------


def _sold_below_row(
    *,
    listing_key: str,
    address: str,
    city: str,
    property_sub_type: str | None,
    bedrooms: int | None,
    bathrooms: int | None,
    close_price: float,
    close_date: str,
    previous_close_price: float,
    previous_close_date: str,
    loss_amount: float,
    image_url: str | None = None,
) -> dict[str, Any]:
    return {
        "listing_key": listing_key,
        "address": address,
        "city": city,
        "property_sub_type": property_sub_type or "",
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "close_price": close_price,
        "close_date": close_date,
        "previous_close_price": previous_close_price,
        "previous_close_date": previous_close_date,
        "loss_amount": loss_amount,
        "image_url": image_url,
    }


def _sold_below_from_table(limit: int, cities: list[str] | None) -> tuple[list[dict[str, Any]], str | None]:
    from homepage.models import SoldBelowPurchase

    qs = SoldBelowPurchase.objects.all()
    if cities:
        city_q = Q()
        for city in cities:
            city_q |= Q(city__iexact=city) | Q(city__istartswith=city)
        qs = qs.filter(city_q)
    rows = list(qs.order_by("-close_date")[:limit])
    photos = _photos(row.listing_key for row in rows)
    return (
        [
            _sold_below_row(
                listing_key=row.listing_key,
                address=row.address,
                city=row.city,
                property_sub_type=row.property_sub_type,
                bedrooms=row.bedrooms,
                bathrooms=row.bathrooms,
                close_price=float(row.close_price),
                close_date=row.close_date.isoformat(),
                previous_close_price=float(row.previous_close_price),
                previous_close_date=row.previous_close_date.isoformat(),
                loss_amount=float(row.loss_amount),
                image_url=photos.get(row.listing_key),
            )
            for row in rows
        ],
        rows[0].computed_at.isoformat() if rows else None,
    )


def _sold_below_from_local(limit: int, cities: list[str] | None) -> list[dict[str, Any]]:
    """Repeat-sale losses from Closed rows already in the catalogue."""
    from homepage.services.sold_below import find_repeat_sale_losses

    qs = Property.objects.filter(
        standard_status__iexact="Closed",
        close_price__gt=0,
        close_date__isnull=False,
        unparsed_address__isnull=False,
    ).exclude(unparsed_address="")
    if cities:
        city_q = Q()
        for city in cities:
            city_q |= Q(city__istartswith=city)
        qs = qs.filter(city_q)
    raw = [
        {
            "ListingKey": row["listing_key"],
            "UnparsedAddress": row["unparsed_address"],
            "City": row["city"] or "",
            "PropertySubType": row["property_sub_type"] or "",
            "BedroomsTotal": row["bedrooms_total"],
            "BathroomsTotalInteger": row["bathrooms_total_integer"],
            "ClosePrice": float(row["close_price"]),
            "CloseDate": row["close_date"].isoformat(),
        }
        for row in qs.order_by("-close_date").values(
            "listing_key",
            "unparsed_address",
            "city",
            "property_sub_type",
            "bedrooms_total",
            "bathrooms_total_integer",
            "close_price",
            "close_date",
        )[:12_000]
    ]
    losses = find_repeat_sale_losses(raw)[:limit]
    photos = _photos(loss.listing_key for loss in losses)
    return [
        _sold_below_row(
            listing_key=loss.listing_key,
            address=loss.address,
            city=loss.city,
            property_sub_type=loss.property_sub_type,
            bedrooms=loss.bedrooms,
            bathrooms=loss.bathrooms,
            close_price=loss.close_price,
            close_date=loss.close_date.isoformat(),
            previous_close_price=loss.previous_close_price,
            previous_close_date=loss.previous_close_date.isoformat(),
            loss_amount=loss.previous_close_price - loss.close_price,
            image_url=photos.get(loss.listing_key),
        )
        for loss in losses
    ]


def _sold_below_from_ampre(limit: int, cities: list[str] | None) -> list[dict[str, Any]]:
    """One-city AMPRE pull when the table and local catalogue have no pairs."""
    from homepage.services.sold_below import find_repeat_sale_losses

    targets = cities[:2] if cities else list(GTA_CITIES[:3])
    cache_key = f"home:sold-below:live:{','.join(c.lower() for c in targets)}:{limit}"

    def build() -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for city in targets:
            try:
                raw.extend(ampre.fetch_closed_sales(city, days=730, max_rows=8_000))
            except AmpreClientError as exc:
                logger.warning("Sold-below live AMPRE miss for %s: %s", city, exc)
        losses = find_repeat_sale_losses(raw)[:limit]
        photos = _photos(loss.listing_key for loss in losses)
        return [
            _sold_below_row(
                listing_key=loss.listing_key,
                address=loss.address,
                city=loss.city,
                property_sub_type=loss.property_sub_type,
                bedrooms=loss.bedrooms,
                bathrooms=loss.bathrooms,
                close_price=loss.close_price,
                close_date=loss.close_date.isoformat(),
                previous_close_price=loss.previous_close_price,
                previous_close_date=loss.previous_close_date.isoformat(),
                loss_amount=loss.previous_close_price - loss.close_price,
                image_url=photos.get(loss.listing_key),
            )
            for loss in losses
        ]

    return _cached(cache_key, 60 * 60, build)


def sold_below_purchase(
    limit: int = 3,
    city: str | None = None,
    cities: list[str] | None = None,
) -> dict[str, Any]:
    preferred = [c for c in (cities or ([city] if city else [])) if c]
    rows, computed_at = _sold_below_from_table(limit, preferred or None)
    source = "table"
    if len(rows) < limit:
        local = _sold_below_from_local(limit, preferred or None)
        if local:
            seen = {row["listing_key"] for row in rows}
            for row in local:
                if row["listing_key"] in seen:
                    continue
                rows.append(row)
                seen.add(row["listing_key"])
                if len(rows) >= limit:
                    break
            source = "table+local" if computed_at else "local"
            computed_at = computed_at or timezone.now().isoformat()
    if len(rows) < max(1, limit // 2):
        try:
            live = _sold_below_from_ampre(limit, preferred or None)
        except Exception as exc:  # noqa: BLE001 — feed must degrade, not 500
            logger.warning("Sold-below AMPRE fill failed: %s", exc)
            live = []
        if live:
            seen = {row["listing_key"] for row in rows}
            for row in live:
                if row["listing_key"] in seen:
                    continue
                rows.append(row)
                seen.add(row["listing_key"])
                if len(rows) >= limit:
                    break
            source = f"{source}+ampre" if rows else "ampre"
            computed_at = computed_at or timezone.now().isoformat()
    return {
        "results": rows[:limit],
        "computed_at": computed_at,
        "source": source,
        "cities": preferred,
    }


# --------------------------------------------------------------------------
# Market snapshot (row kept fresh by a job; recomputed on demand if stale)
# --------------------------------------------------------------------------


def refresh_market_snapshot(scope: str = "gta"):
    from homepage.models import MarketSnapshot

    rows: list[dict[str, Any]] = []
    for city in GTA_CITIES:
        rows.extend(ampre.fetch_closed_sales(city, days=62, max_rows=20000))
    active = Property.objects.filter(standard_status__iexact="active", list_price__gt=0).filter(_gta_q()).count()
    payload = build_snapshot(rows, timezone.now().date(), active)
    snapshot, _ = MarketSnapshot.objects.update_or_create(
        scope=scope, defaults={"payload": payload, "as_of": timezone.now()}
    )
    return snapshot


def market_snapshot(scope: str = "gta") -> dict[str, Any]:
    """Serve the stored snapshot; the ``refresh_market_snapshot`` job keeps it
    fresh. Aggregating 13 cities of AMPRE sales takes tens of seconds, so a
    visitor's request only computes it when no snapshot exists at all — a
    stale one is served with ``stale: true`` instead of blocking the page."""
    from homepage.models import MarketSnapshot

    snapshot = MarketSnapshot.objects.filter(scope=scope).first()
    if snapshot is None:
        try:
            snapshot = refresh_market_snapshot(scope)
        except AmpreClientError as exc:
            raise FeedUnavailable(str(exc)) from exc
    age = (timezone.now() - snapshot.as_of).total_seconds()
    return {**snapshot.payload, "scope": scope, "as_of": snapshot.as_of.isoformat(), "stale": age > SNAPSHOT_MAX_AGE}

# --------------------------------------------------------------------------
# Neighbour activity preview (Just Listed / Price Drop / Open House / Sold)
# --------------------------------------------------------------------------


def neighbour_activity(limit: int = 4, cities: list[str] | None = None) -> dict[str, Any]:
    """Mixed recent activity for the homepage neighbour-alerts panel.

    Prefers visitor cities when provided; otherwise the wider GTA catalogue.
    Builds a diverse set (Just Listed / Price Drop / Open House / Just Sold)
    then backfills from whichever streams still have rows so the rail always
    aims for ``limit`` cards — even when Active inventory is thin and only
    Closed sales are available.
    """
    from mls.models import OpenHouse
    from mls.services.query_helpers import STATUS_GROUPS

    preferred = [c for c in (cities or []) if c]
    city_q = Q()
    for city in preferred:
        city_q |= Q(city__istartswith=city)
    scope_q = city_q if preferred else _gta_q()
    gta_q = _gta_q()

    today = timezone.now().date()
    active_status_q = Q()
    for status_value in STATUS_GROUPS["active"]:
        active_status_q |= Q(standard_status__iexact=status_value)
    sold_status_q = Q()
    for status_value in STATUS_GROUPS["sold"]:
        sold_status_q |= Q(standard_status__iexact=status_value)

    # Sale-priced homes only — monthly rents often share list_price.
    sale_priced = Q(
        list_price__gte=MIN_SALE_PRICE,
        lease_amount__isnull=True,
        total_actual_rent__isnull=True,
    )

    def scoped_values(base_qs, order: str, values: tuple[str, ...], take: int) -> list[dict[str, Any]]:
        """Prefer visitor cities, then fall back to GTA so personalization never blanks."""
        take = max(take, 0)
        if take == 0:
            return []
        rows = list(base_qs.filter(scope_q).order_by(order).values(*values)[:take])
        if len(rows) >= take or not preferred:
            return rows
        seen_keys = {row.get("listing_key") for row in rows}
        for row in base_qs.filter(gta_q).order_by(order).values(*values)[: take * 2]:
            key = row.get("listing_key")
            if not key or key in seen_keys:
                continue
            rows.append(row)
            seen_keys.add(key)
            if len(rows) >= take:
                break
        return rows[:take]

    listed_rows = scoped_values(
        Property.objects.filter(
            active_status_q,
            sale_priced,
            original_entry_timestamp__isnull=False,
        ),
        "-original_entry_timestamp",
        ("listing_key", "unparsed_address", "city", "list_price", "original_entry_timestamp"),
        limit,
    )
    drop_rows = scoped_values(
        Property.objects.filter(
            active_status_q,
            sale_priced,
            previous_list_price__gte=MIN_SALE_PRICE,
            price_change_timestamp__isnull=False,
            list_price__lt=F("previous_list_price"),
        ),
        "-price_change_timestamp",
        (
            "listing_key",
            "unparsed_address",
            "city",
            "list_price",
            "previous_list_price",
            "price_change_timestamp",
        ),
        limit,
    )

    oh_base = OpenHouse.objects.filter(
        date__gte=today,
        property__list_price__gte=MIN_SALE_PRICE,
        property__lease_amount__isnull=True,
        property__total_actual_rent__isnull=True,
    ).filter(
        # Open houses for active-status homes when available.
        Q(property__standard_status__iexact="Active")
        | Q(property__standard_status__iexact="Coming Soon")
        | Q(property__standard_status__iexact="Pending")
    )
    oh_scope = Q()
    for city in preferred or list(GTA_CITIES):
        oh_scope |= Q(property__city__istartswith=city)
    open_house_rows: list[dict[str, Any]] = []
    oh_list = list(
        oh_base.filter(oh_scope).select_related("property").order_by("date", "start_time")[:limit]
    )
    if not oh_list and preferred:
        gta_oh = Q()
        for city in GTA_CITIES:
            gta_oh |= Q(property__city__istartswith=city)
        oh_list = list(
            oh_base.filter(gta_oh).select_related("property").order_by("date", "start_time")[:limit]
        )
    for open_house in oh_list:
        prop = open_house.property
        if not prop:
            continue
        open_house_rows.append(
            {
                "listing_key": prop.listing_key,
                "unparsed_address": prop.unparsed_address,
                "city": prop.city,
                "list_price": prop.list_price,
                "occurred_at": open_house.date,
            }
        )

    sold_rows = scoped_values(
        Property.objects.filter(
            sold_status_q,
            close_price__gt=0,
            close_date__isnull=False,
            # Exclude absurd future close dates that float to the top of -close_date.
            close_date__lte=today,
        ),
        "-close_date",
        ("listing_key", "unparsed_address", "city", "close_price", "close_date"),
        max(limit, 8),
    )

    streams: list[tuple[str, list[dict[str, Any]], str, str]] = [
        ("Just Listed", listed_rows, "original_entry_timestamp", "list_price"),
        ("Price Drop", drop_rows, "price_change_timestamp", "list_price"),
        ("Open House", open_house_rows, "occurred_at", "list_price"),
        ("Just Sold", sold_rows, "close_date", "close_price"),
    ]

    picked: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(event: str, prop: dict[str, Any], *, occurred_at, price: float | None) -> bool:
        key = prop.get("listing_key") or ""
        if not key or key in seen or len(picked) >= limit:
            return False
        seen.add(key)
        picked.append(
            {
                "listing_key": key,
                "event": event,
                "address": (prop.get("unparsed_address") or "").split(",")[0].strip() or key,
                "city": base_city(prop.get("city") or ""),
                "list_price": price,
                "occurred_at": occurred_at.isoformat()
                if hasattr(occurred_at, "isoformat")
                else occurred_at,
                "image_url": None,
            }
        )
        return True

    # Pass 1: one card per event type for variety.
    for event, rows, when_key, price_key in streams:
        if not rows:
            continue
        row = rows[0]
        add(
            event,
            row,
            occurred_at=row.get(when_key) or row.get("occurred_at"),
            price=to_float(row.get(price_key)),
        )

    # Pass 2: round-robin the remaining rows so we always fill to ``limit``.
    indexes = {event: 1 for event, _, _, _ in streams}
    while len(picked) < limit:
        progressed = False
        for event, rows, when_key, price_key in streams:
            idx = indexes[event]
            while idx < len(rows) and len(picked) < limit:
                row = rows[idx]
                idx += 1
                if add(
                    event,
                    row,
                    occurred_at=row.get(when_key) or row.get("occurred_at"),
                    price=to_float(row.get(price_key)),
                ):
                    progressed = True
                    break
            indexes[event] = idx
            if len(picked) >= limit:
                break
        if not progressed:
            break

    photos = _photos(item["listing_key"] for item in picked)
    for item in picked:
        item["image_url"] = photos.get(item["listing_key"])

    return {"results": picked[:limit], "cities": preferred}
