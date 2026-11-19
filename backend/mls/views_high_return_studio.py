"""Staff pin/unpin for the homepage High return MLS rail."""

from __future__ import annotations

from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from vlog.permissions import IsStaffUser

from .models import Property


def _row(prop: Property) -> dict:
    return {
        "listing_key": prop.listing_key,
        "address": prop.unparsed_address or "",
        "city": prop.city or "",
        "list_price": str(prop.list_price) if prop.list_price is not None else None,
        "standard_status": prop.standard_status or "",
        "is_high_return": prop.is_high_return,
        "high_return_order": prop.high_return_order,
        "high_return_until": prop.high_return_until.isoformat() if prop.high_return_until else None,
    }


def clear_investor_cache() -> None:
    """Homepage investor feed is keyed by limit (1–12) and optional city."""
    keys = [f"home:investor:{limit}:" for limit in range(1, 13)]
    cache.delete_many(keys)


class StudioHighReturnAPIView(APIView):
    """GET lists pins plus a search. PATCH sets is_high_return, order, and an optional end time."""

    permission_classes = [IsStaffUser]

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()[:120]
        pins = Property.objects.filter(is_high_return=True).order_by(
            "high_return_order", "-listing_key"
        )[:24]
        results = []
        if query:
            results = (
                Property.objects.filter(
                    Q(unparsed_address__icontains=query)
                    | Q(city__icontains=query)
                    | Q(listing_key__icontains=query)
                )
                .order_by("-list_price")[:20]
            )
        return Response({
            "pins": [_row(prop) for prop in pins],
            "results": [_row(prop) for prop in results],
        })

    def patch(self, request):
        listing_key = request.data.get("listing_key")
        if not isinstance(listing_key, str) or not listing_key.strip():
            return Response({"detail": "Choose a listing."}, status=status.HTTP_400_BAD_REQUEST)
        prop = get_object_or_404(Property, listing_key=listing_key.strip())
        pinned = request.data.get("is_high_return")
        if not isinstance(pinned, bool):
            return Response(
                {"detail": "Set is_high_return to true or false."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order = request.data.get("high_return_order")
        until_raw = request.data.get("high_return_until")
        until = None
        if pinned:
            if order is not None and order != "":
                if isinstance(order, bool) or not isinstance(order, int) or order < 1 or order > 99:
                    return Response(
                        {"detail": "Position must be a number from 1 to 99, or blank."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                order = None
            if until_raw:
                until = parse_datetime(str(until_raw))
                if until is None:
                    return Response(
                        {"detail": "The end date is not valid."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        else:
            order = None
            until = None
        prop.is_high_return = pinned
        prop.high_return_order = order
        prop.high_return_until = until
        prop.save(update_fields=["is_high_return", "high_return_order", "high_return_until"])
        clear_investor_cache()
        return Response(_row(prop))
