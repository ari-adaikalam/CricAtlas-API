from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cricket_api import caching, freshness
from cricket_api.http import respond, strict
from cricket_api.models import Freshness, Health, MetaResponse, Status
from cricket_api.openapi import errors

router = APIRouter()


@router.get("/health", summary="Liveness: the process is up", response_model=Health)
async def health() -> JSONResponse:
    return JSONResponse(Health(status="ok").model_dump(), headers={"Cache-Control": "no-store"})


@router.get("/status", summary="Data age and request counters, nothing about the host", response_model=Status, responses=errors())
async def status(request: Request) -> JSONResponse:
    state = request.app.state
    stats = dict(state.stats)                 # the counters only. How full the limiter is stays inside the process: it tells a caller how close the table is to its bound
    body = Status.model_validate({"data_version": state.db.data_version, "data_built_utc": state.db.meta["built_at_utc"], "snapshot_date": state.db.meta["snapshot_date"], **stats})
    return JSONResponse(body.model_dump(), headers={"Cache-Control": "no-store"})


@router.get("/v1/meta", summary="The data version, row counts, features and the source and licence", response_model=MetaResponse, responses=errors())
async def meta(request: Request):
    strict(request, set())
    m = request.app.state.db.meta
    return respond(request, MetaResponse, {"data": {
        "api_version": request.app.version, "data_version": m["data_version"], "data_built_utc": m["built_at_utc"], "snapshot_date": m["snapshot_date"],
        "coverage": request.app.state.dataset_summary,
        "features": m["features"], "tables": {name: {"rows": t["rows"], "columns": [c.replace(" ", "_") for c in t["columns"]]} for name, t in m["tables"].items()},
        "source": m["source"], "source_url": m["source_url"], "licence": m["licence"], "licence_url": m["licence_url"], "attribution": m["attribution"]}})


@router.get("/freshness", summary="Served data coverage and the last recorded source check", response_model=Freshness, responses=errors())
async def freshness_document(request: Request):
    strict(request, set())
    state = request.app.state
    m = state.db.meta
    body = Freshness.model_validate({
        "data_version": state.db.data_version, "data_built_utc": m["built_at_utc"],
        "snapshot_date": m["snapshot_date"], "coverage": state.dataset_summary,
        "refresh": freshness.refresh_record(state.settings.refresh_status_file, state.db.data_version)})
    return JSONResponse(body.model_dump(), headers={"Cache-Control": "no-store"})


@router.get("/openapi.json", include_in_schema=False)
async def openapi_document(request: Request):
    """The OpenAPI document, built once at start, with the same cache policy and conditional requests as the rest of the reference."""
    return caching.conditional(request, request.app.state.openapi_bytes, "application/json")
