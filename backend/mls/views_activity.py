"""Pre-con activity beacons and the Studio activity report (scope #26)."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from mls.models import (
    AIAssistLog,
    AISearchLog,
    ListingSubmission,
    ListingViewEvent,
    PreconActivityEvent,
    PreComProperty,
    Property,
    PropertyInquiry,
    SearchEvent,
)
from mls.services.request_meta import client_ip, user_agent
from vlog.permissions import IsStaffUser

try:
    from homepage.models import NearbyAlert, NewsletterSubscriber
except Exception:  # pragma: no cover - homepage app is part of this project
    NearbyAlert = None
    NewsletterSubscriber = None

EVENTS = {choice for choice, _label in PreconActivityEvent.EVENT_CHOICES}


class ActivityAnonThrottle(AnonRateThrottle):
    scope = "precon_activity_anon"


class ActivityUserThrottle(UserRateThrottle):
    scope = "precon_activity_user"


class PreconActivityAPIView(APIView):
    """POST /api/mls/activity/precon/ — one view or button click. Best-effort."""

    permission_classes = [AllowAny]
    throttle_classes = [ActivityAnonThrottle, ActivityUserThrottle]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        event = str(data.get("event") or "").strip()
        if event not in EVENTS:
            return Response({"error": "Unknown event."}, status=400)
        try:
            project_id = int(data.get("project_id"))
        except (TypeError, ValueError):
            return Response({"error": "project_id is required."}, status=400)
        if not PreComProperty.objects.filter(pk=project_id).exists():
            return Response({"error": "Project not found."}, status=404)

        session_key = str(data.get("session_key") or "")[:64]
        PreconActivityEvent.objects.create(
            project_id=project_id,
            event=event,
            user=request.user if request.user.is_authenticated else None,
            session_key=session_key,
            ip_address=client_ip(request),
            user_agent=user_agent(request),
        )
        return Response({"ok": True}, status=201)


def _window_days(raw) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 30
    return max(1, min(days, 90))


def _email(user) -> str | None:
    if user is None:
        return None
    return getattr(user, "email", None) or None


def _money(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _property_snapshot(prop: Property | None, listing_key: str = "") -> dict:
    """Human-readable listing fields for Studio activity detail rows."""
    key = (getattr(prop, "listing_key", None) or listing_key or "").strip()
    if prop is None:
        return {
            "listing_key": key or None,
            "mls": None,
            "address": key or "Unknown listing",
            "city": None,
            "neighbourhood": None,
            "price": None,
            "beds": None,
            "baths": None,
            "property_type": None,
            "status": None,
            "detail": key or "Listing not in catalogue",
            "href": f"/property/{key}" if key else None,
        }

    address = (prop.unparsed_address or "").strip() or key or "Address not disclosed"
    city = (prop.city or "").strip() or None
    neighbourhood = (prop.city_region or prop.subdivision_name or "").strip() or None
    mls = (prop.listing_id or "").strip() or key or None
    price = _money(prop.list_price) or _money(prop.lease_amount) or _money(
        getattr(prop, "total_actual_rent", None)
    )
    beds = prop.bedrooms_total
    baths = prop.bathrooms_total_integer
    ptype = (prop.structure_type or prop.property_sub_type or "").strip() or None
    status = (prop.standard_status or "").strip() or None

    bits = []
    if city:
        bits.append(city)
    if neighbourhood and neighbourhood != city:
        bits.append(neighbourhood)
    if price is not None:
        bits.append(f"${price:,.0f}")
    if beds is not None:
        bits.append(f"{beds} bed")
    if baths is not None:
        bits.append(f"{baths} bath")
    if ptype:
        bits.append(ptype)
    if mls:
        bits.append(f"MLS® {mls}")
    if status:
        bits.append(status)

    return {
        "listing_key": key or None,
        "mls": mls,
        "address": address,
        "city": city,
        "neighbourhood": neighbourhood,
        "price": price,
        "beds": beds,
        "baths": baths,
        "property_type": ptype,
        "status": status,
        "detail": " · ".join(bits) if bits else address,
        "href": f"/property/{key}" if key else None,
    }


def _props_by_key(keys: list[str]) -> dict[str, Property]:
    clean = [key for key in {k.strip() for k in keys if k and str(k).strip()}]
    if not clean:
        return {}
    return {
        row.listing_key: row
        for row in Property.objects.filter(listing_key__in=clean).only(
            "listing_key",
            "listing_id",
            "unparsed_address",
            "city",
            "city_region",
            "subdivision_name",
            "list_price",
            "lease_amount",
            "total_actual_rent",
            "bedrooms_total",
            "bathrooms_total_integer",
            "structure_type",
            "property_sub_type",
            "standard_status",
        )
    }


class StudioActivityAPIView(APIView):
    """GET /api/mls/studio/activity/?days=30&user_id= — staff report."""

    permission_classes = [IsStaffUser]

    def get(self, request):
        days = _window_days(request.query_params.get("days"))
        since = timezone.now() - timedelta(days=days)
        # Accept numeric id, email, or username — Studio used to strip non-digits
        # so filtering by email looked like a no-op page reload.
        user_raw = (
            request.query_params.get("user_id")
            or request.query_params.get("user")
            or ""
        ).strip()
        user = None
        if user_raw:
            User = get_user_model()
            user = None
            if user_raw.isdigit():
                user = User.objects.filter(pk=int(user_raw)).first()
            if user is None:
                user = User.objects.filter(email__iexact=user_raw).first()
            if user is None:
                user = User.objects.filter(username__iexact=user_raw).first()
            if user is None:
                return Response(
                    {"error": "User not found. Try their email, username, or numeric id."},
                    status=404,
                )

        listing_qs = ListingViewEvent.objects.filter(created_at__gte=since)
        precon_qs = PreconActivityEvent.objects.filter(created_at__gte=since)
        search_qs = SearchEvent.objects.filter(created_at__gte=since)
        ai_qs = AISearchLog.objects.filter(created_at__gte=since)
        assist_qs = AIAssistLog.objects.filter(created_at__gte=since)
        inquiry_qs = PropertyInquiry.objects.filter(created_at__gte=since)
        assignment_qs = ListingSubmission.objects.filter(
            purpose=ListingSubmission.Purpose.ASSIGNMENT,
            submitted_at__gte=since,
        )
        if user is not None:
            listing_qs = listing_qs.filter(user=user)
            precon_qs = precon_qs.filter(user=user)
            search_qs = search_qs.filter(user=user)
            ai_qs = ai_qs.filter(user=user)
            assist_qs = assist_qs.filter(user=user)
            inquiry_qs = inquiry_qs.filter(user=user)
            assignment_qs = assignment_qs.filter(submitted_by=user)

        top_listings = []
        listing_rows = list(
            listing_qs.values("listing_key").annotate(views=Count("id")).order_by("-views")[:8]
        )
        props = {
            row.listing_key: row
            for row in Property.objects.filter(listing_key__in=[item["listing_key"] for item in listing_rows])
        }
        for item in listing_rows:
            prop = props.get(item["listing_key"])
            top_listings.append(
                {
                    "listing_key": item["listing_key"],
                    "views": item["views"],
                    "address": (getattr(prop, "unparsed_address", None) or "") if prop else "",
                    "city": (getattr(prop, "city", None) or "") if prop else "",
                }
            )

        project_rows = list(
            precon_qs.values("project_id", "event").annotate(total=Count("id"))
        )
        by_project: dict[int, dict] = {}
        for row in project_rows:
            bucket = by_project.setdefault(
                row["project_id"],
                {"id": row["project_id"], "views": 0, "floor_plans": 0, "pricing": 0, "contact": 0},
            )
            key = {
                PreconActivityEvent.EVENT_VIEW: "views",
                PreconActivityEvent.EVENT_FLOOR_PLANS: "floor_plans",
                PreconActivityEvent.EVENT_PRICING: "pricing",
                PreconActivityEvent.EVENT_CONTACT: "contact",
            }.get(row["event"])
            if key:
                bucket[key] = row["total"]
        titles = {}
        for project in PreComProperty.objects.filter(pk__in=by_project).select_related("content"):
            content = getattr(project, "content", None)
            titles[project.pk] = (
                (getattr(content, "title", None) or "").strip()
                or (getattr(project, "address", None) or "").strip()
                or f"Project {project.pk}"
            )
        top_projects = sorted(by_project.values(), key=lambda row: row["views"], reverse=True)[:8]
        for row in top_projects:
            row["title"] = titles.get(row["id"], f"Project {row['id']}")

        top_queries = list(
            search_qs.exclude(query="")
            .values("query")
            .annotate(count=Count("id"))
            .order_by("-count")[:8]
        )

        newsletter = 0
        neighbour = 0
        if NewsletterSubscriber is not None and user is None:
            newsletter = NewsletterSubscriber.objects.filter(created_at__gte=since).count()
        elif NewsletterSubscriber is not None and user is not None:
            newsletter = NewsletterSubscriber.objects.filter(created_at__gte=since, user=user).count()
        if NearbyAlert is not None and user is None:
            neighbour = NearbyAlert.objects.filter(created_at__gte=since).count()
        elif NearbyAlert is not None and user is not None:
            neighbour = NearbyAlert.objects.filter(created_at__gte=since, user=user).count()

        listing_rows = list(listing_qs.select_related("user").order_by("-created_at")[:20])
        # PropertyInquiry.project is EstateProject (title/address), not PreComProperty.content.
        inquiry_rows = list(
            inquiry_qs.select_related("user", "project").order_by("-created_at")[:15]
        )
        props = _props_by_key(
            [row.listing_key for row in listing_rows]
            + [row.listing_key for row in inquiry_rows if row.listing_key]
        )

        timeline = []
        for row in listing_rows:
            snap = _property_snapshot(props.get(row.listing_key), row.listing_key)
            timeline.append(
                {
                    "at": row.created_at.isoformat(),
                    "kind": "listing_view",
                    "label": snap["address"],
                    "detail": snap["detail"],
                    "listing_key": snap["listing_key"],
                    "mls": snap["mls"],
                    "address": snap["address"],
                    "city": snap["city"],
                    "neighbourhood": snap["neighbourhood"],
                    "price": snap["price"],
                    "beds": snap["beds"],
                    "baths": snap["baths"],
                    "property_type": snap["property_type"],
                    "status": snap["status"],
                    "href": snap["href"],
                    "user_email": _email(row.user),
                }
            )
        for row in precon_qs.select_related("user", "project__content").order_by("-created_at")[:15]:
            project = row.project if row.project_id else None
            content = getattr(project, "content", None) if project else None
            title = (
                (getattr(content, "title", None) or "").strip()
                or (getattr(project, "address", None) or "").strip()
                or f"Project {row.project_id}"
            )
            address = (getattr(project, "address", None) or "").strip()
            detail_bits = [f"Pre-construction · {row.event.replace('_', ' ')}"]
            if address and address != title:
                detail_bits.append(address)
            developer = (getattr(project, "developer_name", None) or "").strip()
            if developer:
                detail_bits.append(developer)
            timeline.append(
                {
                    "at": row.created_at.isoformat(),
                    "kind": f"precon_{row.event}",
                    "label": title,
                    "detail": " · ".join(detail_bits),
                    "project_id": row.project_id,
                    "address": address or None,
                    "href": f"/preconstruction/{row.project_id}" if row.project_id else None,
                    "user_email": _email(row.user),
                }
            )
        for row in search_qs.select_related("user").order_by("-created_at")[:12]:
            query = (row.query or "").strip() or "Browse search"
            filters = row.filters_json if isinstance(row.filters_json, dict) else {}
            filter_bits = []
            for key in ("city", "type", "home_type", "status", "price_min", "price_max"):
                value = filters.get(key)
                if value not in (None, "", []):
                    filter_bits.append(f"{key}={value}")
            detail_bits = []
            if row.city:
                detail_bits.append(row.city)
            if row.result_count is not None:
                detail_bits.append(f"{row.result_count} result{'s' if row.result_count != 1 else ''}")
            if filter_bits:
                detail_bits.append(", ".join(filter_bits[:6]))
            timeline.append(
                {
                    "at": row.created_at.isoformat(),
                    "kind": "search",
                    "label": query,
                    "detail": " · ".join(detail_bits) if detail_bits else "Site search",
                    "city": row.city or None,
                    "result_count": row.result_count,
                    "user_email": _email(row.user),
                }
            )
        for row in assist_qs.select_related("user").order_by("-created_at")[:10]:
            detail_bits = []
            if row.handoff and row.handoff != AIAssistLog.HANDOFF_NONE:
                detail_bits.append(f"Handoff: {row.handoff}")
            if row.fallback:
                detail_bits.append("Fallback answer")
            if row.model:
                detail_bits.append(row.model)
            timeline.append(
                {
                    "at": row.created_at.isoformat(),
                    "kind": "ai_assist",
                    "label": row.question,
                    "detail": " · ".join(detail_bits) if detail_bits else "AI assistant",
                    "user_email": _email(row.user),
                }
            )
        for row in inquiry_rows:
            snap = (
                _property_snapshot(props.get(row.listing_key), row.listing_key)
                if row.listing_key
                else None
            )
            name = " ".join(part for part in [row.first_name, row.last_name] if part).strip()
            label = snap["address"] if snap else (name or row.email or "Inquiry")
            detail_bits = [f"Intent: {row.get_intent_display()}"]
            if snap:
                detail_bits.append(snap["detail"])
            elif row.project_id and getattr(row, "project", None):
                project = row.project
                project_title = (
                    (getattr(project, "title", None) or "").strip()
                    or (getattr(project, "address", None) or "").strip()
                    or f"Project {row.project_id}"
                )
                detail_bits.append(f"Pre-con: {project_title}")
            if row.message:
                detail_bits.append(row.message.strip()[:140])
            if row.phone:
                detail_bits.append(row.phone)
            timeline.append(
                {
                    "at": row.created_at.isoformat(),
                    "kind": "inquiry",
                    "label": label,
                    "detail": " · ".join(detail_bits),
                    "listing_key": snap["listing_key"] if snap else (row.listing_key or None),
                    "mls": snap["mls"] if snap else None,
                    "address": snap["address"] if snap else None,
                    "city": snap["city"] if snap else None,
                    "href": snap["href"] if snap else None,
                    "intent": row.intent,
                    "message": (row.message or "").strip()[:280] or None,
                    "user_email": row.email,
                }
            )
        timeline.sort(key=lambda item: item["at"], reverse=True)

        return Response(
            {
                "window_days": days,
                "user_id": user.pk if user else None,
                "top_listings": top_listings,
                "top_projects": top_projects,
                "searches": {
                    "count": search_qs.count() + ai_qs.count(),
                    "ai_count": ai_qs.count(),
                    "top_queries": top_queries,
                },
                "leads": {
                    "inquiries": inquiry_qs.count(),
                    "assignment_submissions": assignment_qs.count(),
                    "newsletter": newsletter,
                    "neighbour_alerts": neighbour,
                },
                "timeline": timeline[:40],
            }
        )
