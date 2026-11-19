"""Resolve a visitor's likely city without browser geolocation.

Priority for callers is usually: explicit cities query → search-history cities
→ this helper (platform IP-city headers). The IP city is approximate and
spoofable; use it only to personalize public rails, never for access control.
"""

from __future__ import annotations

# Longer / multi-word names first so "Richmond Hill" wins over a partial match.
_CANONICAL_CITIES = sorted(
    {
        "Richmond Hill",
        "Mississauga",
        "Newmarket",
        "Toronto",
        "Brampton",
        "Vaughan",
        "Markham",
        "Oakville",
        "Pickering",
        "Burlington",
        "Ajax",
        "Whitby",
        "Oshawa",
        "Milton",
        "Aurora",
        "Hamilton",
        "Caledon",
    },
    key=len,
    reverse=True,
)


def canonicalize_city(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    # URL-encoding and pluses from some CDNs.
    text = text.replace("+", " ").replace("%20", " ").strip()
    low = text.lower()
    for city in _CANONICAL_CITIES:
        if city.lower() == low:
            return city
    for city in _CANONICAL_CITIES:
        if city.lower() in low or low in city.lower():
            return city
    # Title-case unknown Ontario-looking tokens (single/multi word, letters only).
    if 2 <= len(text) <= 40 and all(part.isalpha() or part in "-'" for part in text.replace(" ", "")):
        return " ".join(part.capitalize() for part in text.split())
    return None


def parse_cities_param(raw: str | None, *, limit: int = 5) -> list[str]:
    """CSV / pipe list of cities → de-duplicated canonical names."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.replace("|", ",").split(","):
        city = canonicalize_city(part)
        if not city:
            continue
        key = city.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(city)
        if len(out) >= limit:
            break
    return out


def ip_city_from_request(request) -> str | None:
    """City from CDN / platform headers attached to the request."""
    meta = request.META
    candidates = (
        meta.get("HTTP_X_VISITOR_CITY"),
        meta.get("HTTP_X_VERCEL_IP_CITY"),
        meta.get("HTTP_CF_IPCITY"),
        meta.get("HTTP_X_APPENGINE_CITY"),
        meta.get("HTTP_X_GEO_CITY"),
    )
    for raw in candidates:
        city = canonicalize_city(raw)
        if city:
            return city
    return None


def cities_for_request(request, *, limit: int = 5) -> list[str]:
    """Explicit ``cities`` / ``city`` query first, then IP-derived city."""
    params = request.query_params
    explicit = parse_cities_param(params.get("cities") or params.get("city"), limit=limit)
    if explicit:
        return explicit
    ip_city = ip_city_from_request(request)
    return [ip_city] if ip_city else []
