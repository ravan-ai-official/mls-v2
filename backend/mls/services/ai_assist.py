"""On-site assistant (scope #25).

Answers are limited to a snapshot of this site: active listings matching the
visitor's city / beds / type when asked, published pre-construction projects,
approved assignments, and the public pages the site actually has. The model
cannot use the open web, and every link it returns is checked against that
page list.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from django.db.models import Q

from mls.models import Content, ListingSubmission, PreComProperty, Property
from mls.services.openai_client import OpenAIError, chat, is_configured, search_model
from mls.services.query_helpers import city_match_q

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500

CITIES = (
    "Toronto",
    "Mississauga",
    "Vaughan",
    "Markham",
    "Brampton",
    "Oakville",
    "Burlington",
    "Hamilton",
    "Richmond Hill",
    "Pickering",
    "Ajax",
    "Whitby",
    "Oshawa",
    "Milton",
    "Newmarket",
)

ALLOWED_PREFIXES = (
    "/listings",
    "/property",
    "/map-search",
    "/market-trends",
    "/preconstruction",
    "/assignments",
    "/home-evaluation",
    "/sell",
    "/blog",
    "/communities",
    "/watched",
    "/recently-sold",
    "/contact",
    "/schools",
)

PAGES = [
    {"label": "Search listings", "href": "/listings", "about": "Active MLS listings by city, price, beds and type."},
    {"label": "Map search", "href": "/map-search", "about": "Listings on a map, including a drawn search area."},
    {"label": "Market trends", "href": "/market-trends", "about": "Sold prices, days on market and sale-to-list by city."},
    {"label": "Pre-construction", "href": "/preconstruction", "about": "New developer projects, floor plans and pricing."},
    {"label": "Assignments", "href": "/assignments", "about": "Pre-construction contracts offered for assignment."},
    {"label": "Home evaluation", "href": "/home-evaluation", "about": "An estimate from nearby comparable sales."},
    {"label": "List a property", "href": "/sell", "about": "Start a listing or upload an assignment."},
    {"label": "Blog", "href": "/blog", "about": "Guides and market articles."},
    {"label": "Communities", "href": "/communities", "about": "City-by-city listing snapshots."},
    {"label": "Contact", "href": "/contact", "about": "Talk to a HomeAtlas agent."},
    {"label": "Saved homes", "href": "/watched", "about": "Favourites, notes and email alerts. Sign-in required."},
]

HANDOFFS = {"none", "whatsapp", "contact"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string"},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "href": {"type": "string"},
                },
                "required": ["label", "href"],
            },
        },
        "handoff": {"type": "string", "enum": ["none", "whatsapp", "contact"]},
    },
    "required": ["answer", "links", "handoff"],
}

SYSTEM = (
    "You are the HomeAtlas site assistant for Greater Toronto Area real estate. "
    "Answer ONLY from the JSON context you are given. Do not invent prices, "
    "addresses, school ratings or legal advice. "
    "When the visitor asks for homes (beds, condo, city, rent/sale), lead with "
    "the suggested_search link and mention matching sample listings if present. "
    "When they ask for pre-construction, assignments or market trends, use those "
    "context blocks and the matching page links. "
    "If the context does not contain the answer, say so in one sentence and point "
    "to the closest page. "
    "links must use href values from pages, suggested_search, or listing/project/"
    "assignment hrefs in the context. At most 3 links. "
    "Set handoff to whatsapp when the person wants to talk to a person now, "
    "contact when they want an agent to follow up, otherwise none. "
    "Keep the answer under 120 words."
)


@dataclass
class AssistResult:
    answer: str
    links: list[dict] = field(default_factory=list)
    handoff: str = "none"
    fallback: bool = False
    error: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    def as_response(self) -> dict:
        return {
            "answer": self.answer,
            "links": self.links,
            "handoff": self.handoff,
            "fallback": self.fallback,
        }


def _city_in(question: str) -> str | None:
    lowered = question.lower()
    # Longer names first so "Richmond Hill" wins over a partial.
    for city in sorted(CITIES, key=len, reverse=True):
        if city.lower() in lowered:
            return city
    return None


def _parse_intent(question: str) -> dict:
    text = question.lower()
    city = _city_in(question)
    beds = None
    match = re.search(r"(\d+)\s*(?:\+)?\s*(?:bed|br|bedroom)s?\b", text)
    if match:
        beds = int(match.group(1))

    home_type = None
    if any(token in text for token in ("condo", "apartment", "flat")):
        home_type = "Condo"
    elif any(token in text for token in ("townhome", "townhouse", "town house", "town-home")):
        home_type = "Townhome"
    elif "semi" in text:
        home_type = "Semi-Detached"
    elif any(token in text for token in ("detached", "house", "home")):
        # "home" alone is too broad; only treat as detached when paired with buy/sale cues.
        if "detached" in text or "house" in text:
            home_type = "Detached"

    rent = any(token in text for token in ("rent", "rental", "lease", "for lease"))
    wants_precon = any(
        token in text for token in ("pre-con", "precon", "pre construction", "pre-construction", "new build", "developer", "floor plan")
    )
    wants_assignments = "assignment" in text
    wants_market = any(token in text for token in ("market", "trend", "sold data", "days on market", "sale-to-list"))

    listing_cues = (
        "listing",
        "listings",
        "condo",
        "apartment",
        "house",
        "town",
        "bed",
        "buy",
        "sale",
        "rent",
        "lease",
        "home",
        "property",
        "properties",
    )
    wants_listings = any(token in text for token in listing_cues) or beds is not None or home_type is not None
    # "Assignments for sale" is about contracts, not MLS inventory, unless the
    # visitor also named beds / type / condo / rent / etc.
    if wants_assignments and beds is None and home_type is None:
        specific = (
            "listing",
            "listings",
            "condo",
            "apartment",
            "house",
            "town",
            "bed",
            "rent",
            "lease",
            "home",
            "property",
            "properties",
        )
        if not any(token in text for token in specific):
            wants_listings = False

    return {
        "city": city,
        "beds": beds,
        "home_type": home_type,
        "rent": rent,
        "wants_listings": wants_listings,
        "wants_precon": wants_precon,
        "wants_assignments": wants_assignments,
        "wants_market": wants_market,
    }


def _listings_search_href(intent: dict) -> str:
    params: dict[str, str] = {}
    if intent.get("rent"):
        params["tx"] = "rent"
    else:
        params["status"] = "Active"
    if intent.get("city"):
        params["city"] = intent["city"]
    if intent.get("beds"):
        params["beds"] = str(intent["beds"])
    if intent.get("home_type"):
        params["type"] = intent["home_type"]
    return f"/listings?{urlencode(params)}" if params else "/listings"


def _precon_href(intent: dict) -> str:
    if intent.get("city"):
        return f"/preconstruction?{urlencode({'city': intent['city']})}"
    return "/preconstruction"


def _assignments_href(intent: dict) -> str:
    if intent.get("city"):
        return f"/assignments?{urlencode({'city': intent['city']})}"
    return "/assignments"


def _safe_href(href: str) -> str | None:
    raw = (href or "").strip()
    if not raw.startswith("/") or raw.startswith("//"):
        return None
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    allowed = any(path == prefix or path.startswith(prefix + "/") for prefix in ALLOWED_PREFIXES)
    if not allowed:
        return None
    query = f"?{parts.query}" if parts.query else ""
    return f"{path}{query}"[:300]


def _clean_links(links) -> list[dict]:
    cleaned = []
    if not isinstance(links, list):
        return cleaned
    for item in links[:3]:
        if not isinstance(item, dict):
            continue
        href = _safe_href(str(item.get("href") or ""))
        label = str(item.get("label") or "").strip()[:80]
        if href and label:
            cleaned.append({"label": label, "href": href})
    return cleaned


def _home_type_q(home_type: str | None) -> Q | None:
    if not home_type:
        return None
    token = home_type.lower()
    if token == "condo":
        return (
            Q(structure_type__icontains="apartment")
            | Q(property_sub_type__icontains="condo")
            | Q(property_sub_type__icontains="co-op")
            | Q(property_sub_type__icontains="apartment")
        )
    if token == "townhome":
        return (
            Q(structure_type__istartswith="row")
            | Q(property_sub_type__icontains="town")
            | Q(property_sub_type__icontains="att/row")
        )
    if token == "semi-detached":
        return Q(property_sub_type__icontains="semi") | Q(property_attached_yn=True, structure_type__iexact="House")
    if token == "detached":
        return Q(property_sub_type__icontains="detached") | Q(
            structure_type__iexact="House",
            property_attached_yn=False,
        )
    return None


def site_context(question: str) -> dict:
    """A small, real snapshot. Failures become empty lists, never invented rows."""
    intent = _parse_intent(question)
    context: dict = {
        "pages": PAGES,
        "intent": {
            "city": intent["city"],
            "beds": intent["beds"],
            "home_type": intent["home_type"],
            "rent": intent["rent"],
        },
    }

    search_href = _listings_search_href(intent)
    if intent["wants_listings"] or intent["city"]:
        context["suggested_search"] = {
            "label": "See matching listings",
            "href": search_href,
            "note": "Open this page for the full filtered MLS results.",
        }

    if intent["wants_listings"] or intent["city"]:
        try:
            qs = Property.objects.filter(standard_status__iexact="Active")
            if intent["rent"]:
                qs = qs.filter(Q(lease_amount__gt=0) | Q(total_actual_rent__gt=0))
            else:
                qs = qs.filter(lease_amount__isnull=True, total_actual_rent__isnull=True)
            if intent["city"]:
                qs = qs.filter(city_match_q(intent["city"]))
            if intent["beds"] is not None:
                qs = qs.filter(bedrooms_total__gte=intent["beds"])
            type_q = _home_type_q(intent["home_type"])
            if type_q is not None:
                qs = qs.filter(type_q)

            total = qs.count()
            rows = list(
                qs.order_by("-modification_timestamp").values(
                    "listing_key",
                    "unparsed_address",
                    "city",
                    "list_price",
                    "lease_amount",
                    "total_actual_rent",
                    "bedrooms_total",
                    "property_sub_type",
                )[:5]
            )
            context["active_listings"] = {
                "city": intent["city"],
                "match_count": total,
                "sample": [
                    {
                        "address": row["unparsed_address"] or "",
                        "city": row["city"] or intent["city"] or "",
                        "list_price": float(row["list_price"]) if row["list_price"] is not None else None,
                        "lease_amount": float(row["lease_amount"] or row["total_actual_rent"] or 0) or None,
                        "beds": row["bedrooms_total"],
                        "type": row["property_sub_type"] or "",
                        "href": f"/property/{row['listing_key']}",
                    }
                    for row in rows
                    if row.get("listing_key")
                ],
                "results_href": search_href,
                "note": "Sample of matching active listings. Use results_href for the full set.",
            }
        except Exception:
            logger.exception("assist listing context failed")
            context["active_listings"] = {
                "city": intent["city"],
                "match_count": 0,
                "sample": [],
                "results_href": search_href,
            }

    if intent["wants_precon"] or not intent["wants_listings"]:
        try:
            projects = (
                PreComProperty.objects.filter(content__status=Content.PUBLISH)
                .select_related("content")
                .order_by("-is_featured", "content__title")
            )
            if intent["city"]:
                city = intent["city"]
                projects = projects.filter(
                    Q(address__icontains=city) | Q(content__title__icontains=city) | Q(developer_name__icontains=city)
                )
            projects = list(projects[:6])
            context["preconstruction"] = [
                {
                    "title": project.content.title,
                    "address": (project.address or "")[:120],
                    "href": f"/preconstruction/{project.pk}-{project.content.slug}",
                }
                for project in projects
            ]
            context["preconstruction_href"] = _precon_href(intent)
        except Exception:
            logger.exception("assist precon context failed")
            context["preconstruction"] = []

    if intent["wants_assignments"] or (not intent["wants_listings"] and not intent["wants_precon"]):
        try:
            assignments = ListingSubmission.objects.filter(
                status=ListingSubmission.Status.APPROVED,
                purpose=ListingSubmission.Purpose.ASSIGNMENT,
            )
            if intent["city"]:
                assignments = assignments.filter(city__icontains=intent["city"])
            assignments = list(assignments.order_by("-submitted_at")[:5])
            context["assignments"] = [
                {
                    "address": f"{row.address_line_1}, {row.city}",
                    "asking_price": float(row.asking_price) if row.asking_price is not None else None,
                    "href": f"/assignments/{row.pk}",
                }
                for row in assignments
            ]
            context["assignments_href"] = _assignments_href(intent)
        except Exception:
            logger.exception("assist assignment context failed")
            context["assignments"] = []

    if intent["wants_market"]:
        href = f"/market-trends?{urlencode({'city': intent['city']})}" if intent["city"] else "/market-trends"
        context["market_trends"] = {
            "href": href,
            "note": "Open Market trends for sold prices, DOM and sale-to-list charts.",
        }

    return context


def _keyword_links(question: str) -> list[dict]:
    intent = _parse_intent(question)
    picks: list[dict] = []

    if intent["wants_listings"] or intent["beds"] or intent["home_type"] or (
        intent["city"] and not intent["wants_precon"] and not intent["wants_assignments"] and not intent["wants_market"]
    ):
        picks.append({"label": "See matching listings", "href": _listings_search_href(intent)})
    if intent["wants_precon"]:
        picks.append({"label": "Pre-construction", "href": _precon_href(intent)})
    if intent["wants_assignments"]:
        picks.append({"label": "Assignments", "href": _assignments_href(intent)})
    if intent["wants_market"]:
        href = f"/market-trends?{urlencode({'city': intent['city']})}" if intent["city"] else "/market-trends"
        picks.append({"label": "Market trends", "href": href})

    if not picks:
        text = question.lower()
        rules = (
            (("map",), PAGES[1]),
            (("value", "valuation", "worth", "estimate"), PAGES[5]),
            (("sell", "list my"), PAGES[6]),
            (("blog", "article", "news"), PAGES[7]),
            (("contact", "agent", "talk"), PAGES[9]),
        )
        for words, page in rules:
            if any(word in text for word in words):
                picks.append({"label": page["label"], "href": page["href"]})
    if not picks:
        picks.append({"label": PAGES[0]["label"], "href": PAGES[0]["href"]})
    return picks[:3]


def _fallback(question: str, error: str = "") -> AssistResult:
    intent = _parse_intent(question)
    if intent["wants_listings"] or intent["beds"] or intent["home_type"]:
        answer = (
            "I can open a filtered listings search for that. "
            "Use the link below, or ask for a city, beds or home type to narrow it."
        )
        handoff = "none"
    elif intent["wants_precon"]:
        answer = "Browse published pre-construction projects on this site from the link below."
        handoff = "none"
    elif intent["wants_assignments"]:
        answer = "Approved assignment listings are collected on the Assignments page."
        handoff = "none"
    elif intent["wants_market"]:
        answer = "Market trends for GTA cities are on the Market trends page."
        handoff = "none"
    else:
        answer = (
            "I can point you to the right page on this site — listings, "
            "pre-construction, assignments or market trends. "
            "For a person, use WhatsApp or Contact."
        )
        handoff = "contact"

    return AssistResult(
        answer=answer,
        links=_keyword_links(question),
        handoff=handoff,
        fallback=True,
        error=error,
    )


def answer_question(question: str) -> AssistResult:
    question = question.strip()[:MAX_QUESTION_CHARS]
    if not is_configured():
        return _fallback(question, "GEMINI_API_KEY is not configured.")

    context = site_context(question)
    started = time.monotonic()
    try:
        result = chat(
            [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps({"question": question, "context": context}, default=str),
                },
            ],
            model=search_model(),
            json_schema=SCHEMA,
            schema_name="assist",
            max_tokens=500,
            timeout=20,
        )
    except OpenAIError as exc:
        logger.warning("ai assist fallback: %s", exc)
        fallback = _fallback(question, str(exc)[:500])
        fallback.latency_ms = int((time.monotonic() - started) * 1000)
        return fallback

    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(result.content)
    except json.JSONDecodeError:
        fallback = _fallback(question, "Model reply was not JSON.")
        fallback.latency_ms = latency_ms
        fallback.model = result.model
        return fallback

    handoff = payload.get("handoff") if payload.get("handoff") in HANDOFFS else "none"
    answer = str(payload.get("answer") or "").strip()[:1500]
    if not answer:
        fallback = _fallback(question, "Model reply had an empty answer.")
        fallback.latency_ms = latency_ms
        return fallback

    links = _clean_links(payload.get("links"))
    # Ensure intent chips always keep a useful on-site link even if the model
    # omits one.
    intent = _parse_intent(question)
    if (intent["wants_listings"] or intent["beds"] or intent["home_type"]) and not any(
        link["href"].startswith("/listings") for link in links
    ):
        links = ([{"label": "See matching listings", "href": _listings_search_href(intent)}] + links)[:3]
    if intent["wants_precon"] and not any(link["href"].startswith("/preconstruction") for link in links):
        links = ([{"label": "Pre-construction", "href": _precon_href(intent)}] + links)[:3]
    if intent["wants_assignments"] and not any(link["href"].startswith("/assignments") for link in links):
        links = ([{"label": "Assignments", "href": _assignments_href(intent)}] + links)[:3]
    if intent["wants_market"] and not any(link["href"].startswith("/market-trends") for link in links):
        market_href = (
            f"/market-trends?{urlencode({'city': intent['city']})}" if intent["city"] else "/market-trends"
        )
        links = ([{"label": "Market trends", "href": market_href}] + links)[:3]

    return AssistResult(
        answer=answer,
        links=links,
        handoff=handoff,
        fallback=False,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=latency_ms,
    )
