"""The cache policy of every route, and the rules for conditional requests. Written once, used by the server and by the documentation.

Every answer that can be cached has an ETag made from the answer itself and the data version, so it changes when the data does and when anything in the request changes the answer.
The cache key is the whole address with its query string, so a different filter, field selection, page or cursor is a different entry.

Policy by kind of route:

  match      a finished match and its parts. It changes only when Cricsheet corrects it, and a new data version changes the ETag, so it may be reused for a day.
  computed   statistics, records, rankings, searches and lists. They change as matches are added, so they are reused for an hour.
  reference  the metadata, the documentation and the OpenAPI document, which change with the data version or a release. An hour.
  no-store   /health and /status, which describe the moment, and every error and every refusal, which must never be replayed.

A shared cache (a CDN) must not replay what belongs to one client or one request. A server behind one says so with CRICKET_API_SHARED_CACHE, and then a cacheable answer carries none of the
rate limit headers, X-Request-ID or X-Response-Time-Ms. Those stay on every answer sent directly, and on every 429 and error, which are never cached.
"""
import hashlib
import re
from dataclasses import dataclass

from fastapi import Request, Response


@dataclass(frozen=True)
class Policy:
    name: str
    max_age: int | None            # None means the answer is never stored
    for_routes: str


MATCH = Policy("match", 86400, "a match, its scorecard, its balls and its powerplays, reviews, replacements, wickets and notes")
COMPUTED = Policy("computed", 3600, "statistics, splits, leaderboards, matchups, team and head-to-head records, match and player histories, searches, and the lists of competitions, teams, venues and players")
REFERENCE = Policy("reference", 3600, "`/v1/meta`, `/openapi.json` the documentation page and its stylesheet, and the root icons")
NO_STORE = Policy("no-store", None, "`/health`, `/status`, `/freshness`, and every error and rate limit refusal")
POLICIES = (MATCH, COMPUTED, REFERENCE, NO_STORE)

ONE_MATCH = re.compile(r"^/v1/matches/[^/]+(/.*)?$")
# What belongs to one client or one request, so a shared cache must not replay it.
PER_CLIENT = ("RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "X-Request-ID", "X-Response-Time-Ms")


def policy_for(path: str) -> Policy:
    """The policy of a route, from its path. A template such as /v1/matches/{match_id} and a real address give the same answer."""
    if path in ("/health", "/status", "/freshness"):
        return NO_STORE
    if path in ("/v1/meta", "/openapi.json", "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png") or path == "/docs" or path.startswith("/docs/"):
        return REFERENCE
    if ONE_MATCH.match(path):
        return MATCH
    return COMPUTED


def cache_control(policy: Policy) -> str:
    return "no-store" if policy.max_age is None else f"public, max-age={policy.max_age}"


def etag_for(body: bytes, data_version: str) -> str:
    return 'W/"' + hashlib.sha256(body + data_version.encode()).hexdigest()[:20] + '"'


VALIDATOR = r'\s*(?:W/)?"[^"]*"\s*'
LIST = re.compile(rf"\s*\*\s*|{VALIDATOR}(?:,{VALIDATOR})*")


def satisfied(header: str | None, etag: str) -> bool:
    """Whether an If-None-Match header matches the answer, so the reply is a 304 (RFC 9110, section 13.1.2). The header is * (any current answer) alone, or one or more validators separated
    by commas. A weak validator matches a strong one of the same tag (the weak comparison a GET uses). Anything else, including * mixed with tags, matches nothing."""
    if not header or not LIST.fullmatch(header):
        return False
    if header.strip() == "*":
        return True
    weak = lambda tag: tag[2:] if tag.startswith("W/") else tag
    return any(weak(tag.strip()) == weak(etag) for tag in re.findall(VALIDATOR, header))


def conditional(request: Request, body: bytes, media_type: str, extra: dict | None = None) -> Response:
    """The answer, or a 304 with no body when the client already holds it. Both carry the ETag and the cache policy of the route."""
    headers = {"ETag": etag_for(body, request.app.state.db.data_version), "Cache-Control": cache_control(policy_for(request.url.path)), **(extra or {})}
    if satisfied(", ".join(request.headers.getlist("if-none-match")), headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)
