"""The application: the routes, the limits and the headers every response carries."""
import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from cricket_api import admission, clients, faults, freshness, openapi, telemetry
from cricket_api.db import Database
from cricket_api.errors import ApiError, problem
from cricket_api.ratelimit import RateLimiter
from cricket_api.routes import docs, icons, leaderboards, lookups, match_parts, matches, matchups, meta, players, teams
from cricket_api.settings import Settings

API_VERSION = "0.1.0"
EXPOSED = ["ETag", "Link", "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After", "X-Attribution"]      # what a browser script may read, on every response
UNCOUNTED = ("/health",)      # liveness is free, so a monitor can never be limited
HEAVY = ("/balls", "/scorecard", "/batting", "/bowling", "/fielding", "/seasons", "/splits", "/matchups", "/record", "/head-to-head")
# A player's batting, bowling and fielding career, seasons and splits read one small derived table and take 3 to 8 ms of CPU, as much as a list route (measured 2026-10-13, docs/measured_results.md),
# so they cost one token. The match history and matchups take 7 to 10 ms and cost 2. The scorecard (12 to 20 ms), the ball pages (bytes) and the other heavy routes keep 3, and the
# leaderboards (22 to 40 ms) 5. Match detail stays at 1 (owner decision 2026-10-14).
CHEAP_PLAYER_STATS = re.compile(r"^/v1/players/[^/]+/(batting|bowling|fielding)(/seasons|/splits)?$")


def cost_for(path: str) -> int:
    """Tokens a request costs. /health is not counted at all. Leaderboards sum every player's totals, so they cost the most (5). The balls, scorecard and the other heavy routes cost 3, a player's match history
    and matchups 2, and the rest, including a player's batting, bowling and fielding career, seasons and splits, 1."""
    if path in UNCOUNTED:
        return 0
    if "/leaderboards/" in path:
        return 5
    if CHEAP_PLAYER_STATS.match(path):
        return 1
    if path.startswith("/v1/players/") and path.endswith("/matches"):
        return 2
    if path == "/v1/matchups":
        return 2
    return 3 if path.endswith(HEAVY) else 1


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    policy = clients.Policy.from_settings(settings)         # first, so that a bad setting stops the start before any file is opened
    limits = admission.Limits.from_settings(settings)
    db = Database(settings)
    meta_info = db.meta
    app = FastAPI(
        title="Cricket API", version=API_VERSION, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False,
        description=(f"A read-only API over ball-by-ball cricket data. {meta_info['attribution']} "
                     f"Licence: [{meta_info['licence']}]({meta_info['licence_url']}). Source: [{meta_info['source']}]({meta_info['source_url']})."),
        license_info={"name": meta_info["licence"], "url": meta_info["licence_url"]},
        contact={"name": "Ari Adaikalam", "url": "https://ariadaikalam.com", "email": "ariadaikalam1234@gmail.com"})
    app.state.settings, app.state.db = settings, db
    app.state.dataset_summary = freshness.dataset_summary(db)
    app.state.limiter = RateLimiter(settings.rate_per_minute, settings.rate_burst, settings.max_clients)
    app.state.limits, app.state.gate = limits, admission.Gate(limits)
    hsts = f"max-age={settings.hsts_max_age}; includeSubDomains"        # the directive stays as it was, and preload is never sent, see the README
    classifier = admission.Classifier()
    app.state.telemetry = telemetry.Telemetry(classifier, API_VERSION, db.data_version, meta_info.get("built_at_utc"), gate=app.state.gate, db=db)
    app.state.stats = {"started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "requests": 0, "client_errors": 0, "server_errors": 0, "rate_limited": 0}

    # Innermost first: the gate sits inside CORS and the guard, so its refusals get both their headers and the guard's request id.
    app.add_middleware(admission.AdmissionMiddleware, gate=app.state.gate, classifier=classifier, observer=app.state.telemetry.wait, request_seconds=settings.request_seconds)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["If-None-Match"], allow_credentials=False,
                       expose_headers=EXPOSED)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        started = time.perf_counter()
        request.state.request_id = uuid.uuid4().hex[:16]
        stats = app.state.stats
        stats["requests"] += 1
        decision = None
        if request.method not in ("GET", "OPTIONS"):
            response = problem(request, 405, "Only GET is allowed", headers={"Allow": "GET, OPTIONS"})
        elif request.headers.get("content-length", "0") not in ("0", "") or "transfer-encoding" in request.headers:
            response = problem(request, 413, "A request body is not accepted")
        else:
            refused = None
            if request.url.path not in UNCOUNTED and request.method == "GET":
                try:
                    client = policy.identify(request)
                    request.state.client_key = client                   # the admission gate keys its one heavy request per client by the same identity
                    decision = app.state.limiter.take(client, cost_for(request.url.path))
                except clients.ClientAddressError as error:
                    refused = problem(request, error.status, error.detail)
                else:
                    if not decision.allowed:
                        stats["rate_limited"] += 1
                        refused = problem(request, 429, "Too many requests, slow down", headers={"Retry-After": str(decision.retry_after)})
            if refused is not None:
                response = refused
            else:
                try:
                    response = await call_next(request)
                except ApiError as error:
                    response = problem(request, error.status, error.detail, error.headers, error.parameter)
                except Exception as fault:                          # never show a trace, a path or a library name. The operator's log gets them, under the same request id
                    app.state.telemetry.fault(faults.kind_of(fault))        # counted first: every fault is counted, whether or not its log entry is written
                    faults.report(request, fault, started, time.perf_counter())
                    response = problem(request, 500, "Something went wrong")
        if response.status_code >= 500:
            stats["server_errors"] += 1
        elif response.status_code >= 400:
            stats["client_errors"] += 1
        # Behind a shared cache, a cacheable answer carries nothing that belongs to one client or one request, because the cache would replay it to the next client.
        shared = settings.shared_cache and response.headers.get("cache-control", "").startswith("public")
        if decision is not None and not shared:
            response.headers["RateLimit-Limit"] = str(decision.limit)
            response.headers["RateLimit-Remaining"] = str(decision.remaining)
            response.headers["RateLimit-Reset"] = str(decision.reset_seconds)
        if not shared:
            response.headers.update({"X-Request-ID": request.state.request_id, "X-Response-Time-Ms": f"{(time.perf_counter() - started) * 1000:.0f}"})
        if response.status_code >= 400:
            # A refused CORS preflight comes from the middleware below. It carries no Cache-Control and a max age, so a failure could be remembered.
            response.headers.setdefault("Cache-Control", "no-store")
            if "access-control-max-age" in response.headers:
                del response.headers["access-control-max-age"]
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Strict-Transport-Security": hsts,
            "Link": f'<{meta_info["licence_url"]}>; rel="license"', "X-Attribution": f"Data: {meta_info['source']} ({meta_info['source_url']}), {meta_info['licence']}"})
        # The data is public and the API has no cookies or credentials, so every response says the same thing about CORS whichever origin asked, or none: a shared cache may store a response fetched without
        # an Origin and serve it to a browser. The CORS middleware only adds these when an Origin is sent, and answers a request with a Cookie by naming the origin, so they are set here, last.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = ", ".join(EXPOSED)
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")      # the documentation page sets its own
        if "server" in response.headers:
            del response.headers["server"]
        return response

    # Outermost, so it counts every response. Its numbers are served only by the private listener (serve.py), never by a route of this app.
    app.add_middleware(telemetry.TelemetryMiddleware, telemetry=app.state.telemetry)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        return problem(request, error.status, error.detail, error.headers, error.parameter)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        first = error.errors()[0] if error.errors() else {}
        parameter = str(first.get("loc", [""])[-1]) if first.get("loc") else None
        return problem(request, 422, f"Invalid value for the parameter {parameter}" if parameter else "Invalid request", parameter=parameter)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        return problem(request, error.status_code, "Not found" if error.status_code == 404 else "Request refused")

    for module in (meta, lookups, matches, match_parts, players, leaderboards, matchups, teams):
        app.include_router(module.router)
    app.include_router(icons.router)
    app.state.root_icons = icons.load()
    if settings.docs_enabled:
        app.include_router(docs.router)
    openapi.install(app)
    app.openapi()["info"]["x-cricatlas-data"] = {
        "data_version": db.data_version, "data_built_utc": meta_info["built_at_utc"],
        "snapshot_date": meta_info["snapshot_date"], **app.state.dataset_summary}
    # Built now, so that no request has to run a query or a long computation to get them, and the routes that serve them can be plain async handlers.
    app.state.openapi_bytes = json.dumps(app.openapi(), separators=(",", ":"), ensure_ascii=False).encode()
    if settings.docs_enabled:
        from cricket_api.docsite import render
        facts = render.collect(app, db, settings, cost_for)
        app.state.docs_html = render.page(facts).encode("utf-8")
        # The same page and OpenAPI document for the web app's /api, built once with that prefix. They are served only under /_via/web (a path the proxy lets through for the web app alone), so one URL never has two
        # answers and Cloudflare keeps the two apart.
        server = settings.web_origin + "/api"
        app.state.docs_html_web = render.page({**facts, "public_url": settings.web_origin, "server": server, "openapi_path": "/api/openapi.json"}, base="/api").encode("utf-8")
        web_spec = json.loads(json.dumps(app.openapi()))
        web_spec["servers"] = [{"url": server}]
        app.state.openapi_web_bytes = json.dumps(web_spec, separators=(",", ":"), ensure_ascii=False).encode()
        app.state.docs_css = (docs.ASSETS / "docs.css").read_bytes()
        app.state.docs_icons = {name: (docs.ASSETS / name).read_bytes() for name in docs.ICONS}
    return app
