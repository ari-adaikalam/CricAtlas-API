"""The documentation page and its stylesheet, served by the API itself. Nothing is loaded from anywhere else, and the page runs no script.

The page is built once for a data version, from the OpenAPI document and the running server, so what it says about limits, costs and routes is what the server does.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from cricket_api import caching, freshness
from cricket_api.docsite.guide import dl, p

router = APIRouter()
ASSETS = Path(__file__).resolve().parent.parent / "docsite" / "assets"
# A page with no script and no outside address. Styles and the one font come only from this server, and nothing may frame the page or submit a form.
POLICY = "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


# no-transform on the page, and only on the page: Cloudflare's automatic Web Analytics adds a script to every HTML answer it may change, and this page's policy refuses scripts, so the browser
# would report a blocked script on every visit. A response marked no-transform is left as it is (https://developers.cloudflare.com/web-analytics/faq/).
HTML_CACHE_CONTROL = "public, max-age=3600, no-transform"


def respond(request: Request, body: bytes, media_type: str, extra: dict | None = None) -> Response:
    return caching.conditional(request, body, media_type, {"Content-Security-Policy": POLICY, **(extra or {})})


def documentation_page(request: Request, page: bytes) -> Response:
    state = request.app.state
    record = freshness.refresh_record(state.settings.refresh_status_file, state.db.data_version)
    if record["automatic_verified"]:
        note = ("Automatic daily refresh. The source is checked once a day. A check that finds nothing new changes nothing: the data version and its activation time stay as they were. "
                "New data is published only after it has been prepared, verified and activated, and what is available depends on what Cricsheet has published.")
    elif record["record_available"] and record["mode"] == "automatic" and record["scheduler"] == "disabled":
        note = ("The automatic daily refresh is switched off: its schedule is disabled. The times below are from earlier runs and say nothing about now. "
                "New data is not being checked for or published automatically.")
    elif record["record_available"] and record["mode"] == "automatic" and record["scheduler"] in {"unknown", "unrecorded"}:
        note = ("Whether the daily schedule is running cannot be confirmed right now. The times below are from earlier runs and say nothing about now, "
                "so this page does not describe the refresh as automatic.")
    elif record["record_available"] and record["mode"] == "automatic":
        note = ("An automatic refresh is being brought into service. The schedule is running, but a successful scheduled check and a completed publication are both "
                "needed and have not both been seen. Until then this page does not describe the refresh as automatic.")
    elif record["record_available"]:
        note = "Preparation-only record. Automatic publication is not enabled."
    elif state.settings.refresh_status_file:
        note = "The refresh record is unavailable. No successful refresh can be confirmed from it."
    else:
        note = "Automatic daily refresh is not enabled. Refresh times are unavailable until recorded."
    content = (p(note) + dl([
        ("Latest match date", state.dataset_summary["latest_match_date"] or "Unavailable"),
        ("Last successful source check (UTC)", record["last_successful_check_utc"] or "Not recorded"),
        ("Last successful data refresh (UTC)", record["last_successful_refresh_utc"] or "Not recorded"),
        ("Last refresh attempt", record["status"]),
    ])).encode()
    body = page.replace(b"<!--cricatlas-refresh-status-->", content)
    policy = "public, max-age=60, no-transform" if state.settings.refresh_status_file else HTML_CACHE_CONTROL
    return respond(request, body, "text/html; charset=utf-8", {"Cache-Control": policy})


@router.get("/docs", include_in_schema=False)
async def documentation(request: Request):
    return documentation_page(request, request.app.state.docs_html)


# The web variant, for the web app's /api: the same page with the /api prefix on its asset links and share URLs, and the OpenAPI document with the web app's address as its server. The proxy lets these paths
# through only for requests that carry the web app's credential, and the web app maps /api/docs, /api/docs/assets/... and /api/openapi.json onto them.
@router.get("/docs/assets/docs.css", include_in_schema=False)
async def stylesheet(request: Request):
    return respond(request, request.app.state.docs_css, "text/css; charset=utf-8")


# The page's icons (the CricAtlas mark as an SVG, and as PNGs for the browsers that do not draw an SVG icon) and its font (Outfit, the app's typeface). They are served from here and named in the page's
# <link> tags and stylesheet, so no browser has to guess and nothing comes from another address.
ICONS = {"brand-mark.svg": "image/svg+xml", "icon-32.png": "image/png", "apple-touch-icon.png": "image/png", "outfit-latin.woff2": "font/woff2", "og-api-docs.png": "image/png"}


def icon_route(name: str, media_type: str):
    async def icon(request: Request):
        return respond(request, request.app.state.docs_icons[name], media_type)
    router.add_api_route(f"/docs/assets/{name}", icon, methods=["GET"], include_in_schema=False, name=f"docs_icon_{name}")


for _name, _type in ICONS.items():
    icon_route(_name, _type)


# The web variant, for the web app's /api: the same page with the /api prefix on its asset links and share URLs, and the OpenAPI document with the web app's address as its server. The proxy lets these paths
# through only for requests that carry the web app's credential, and the web app maps /api/docs, /api/docs/assets/... and /api/openapi.json onto them. One route for all of them, so it adds one route to the
# telemetry's closed set (about 68 series) and not seven. Only the real names answer; anything else is the standard 404.
@router.get("/_via/web/{rest:path}", include_in_schema=False)
async def web_variant(request: Request, rest: str):
    state = request.app.state
    if rest == "docs":
        return documentation_page(request, state.docs_html_web)
    if rest == "openapi.json":
        return caching.conditional(request, state.openapi_web_bytes, "application/json")
    name = rest.removeprefix("docs/assets/") if rest.startswith("docs/assets/") else ""
    if name == "docs.css":
        return respond(request, state.docs_css, "text/css; charset=utf-8")
    if name in ICONS:
        return respond(request, state.docs_icons[name], ICONS[name])
    raise HTTPException(status_code=404)
