"""Shared helpers for the hero / listings freshness endpoint."""

from __future__ import annotations

from django.core.cache import cache

from mls.models import ListingSyncStatus, Property

CACHE_KEY = "listing-sync-status:v3"
CACHE_TTL_SECONDS = 60


def payload() -> dict:
    sync_status = ListingSyncStatus.objects.filter(key="ddf_properties").first()
    return {
        "last_successful_at": sync_status.last_successful_at if sync_status else None,
        # Rows downloaded by the last run, NOT the catalogue size.
        "listing_count": sync_status.listing_count if sync_status else 0,
        # What the site can show right now: the figure for "N live listings".
        "active_listing_count": Property.objects.filter(
            standard_status__iexact="Active"
        ).count(),
    }


def clear_cache() -> None:
    cache.delete(CACHE_KEY)


def cached_payload() -> dict:
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    body = payload()
    cache.set(CACHE_KEY, body, CACHE_TTL_SECONDS)
    return body
