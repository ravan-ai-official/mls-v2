from celery import shared_task

from mls.services.newsletter_notifications import send_daily_listing_newsletters


@shared_task
def run_daily_listing_newsletters() -> dict:
    return send_daily_listing_newsletters()


@shared_task
def warm_sold_trends_cache() -> dict:
    from mls.views_market import warm_sold_trends

    return warm_sold_trends()


@shared_task
def run_saved_search_alerts() -> dict:
    from mls.services.saved_search_alerts import send_saved_search_alerts

    return send_saved_search_alerts()


@shared_task
def sync_ddf_listing_cache() -> dict:
    """Incremental DDF sync + expire ghost Actives. Enable via DDF_SYNC_BEAT_ENABLED=1."""
    from django.core.management import call_command

    call_command("sync_ddf_properties")
    return {"ok": True}


@shared_task
def sync_ampre_inactive_listings() -> dict:
    """Pull Terminated/Expired/Closed from AMPRE. Enable via AMPRE_CLOSED_BEAT_ENABLED=1."""
    from django.core.management import call_command

    call_command("sync_ampre_closed")
    return {"ok": True}
