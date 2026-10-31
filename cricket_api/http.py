"""Shared pieces for the routes: strict parameters, signed pagination, cached JSON responses."""
import json

from fastapi import Request, Response

from cricket_api import caching, cursors
from cricket_api.errors import ApiError
from cricket_api.models import Model


def source(request: Request) -> dict:
    meta = request.app.state.db.meta
    return {"name": meta["source"], "url": meta["source_url"], "licence": "ODC-By 1.0", "licence_url": meta["licence_url"]}


def strict(request: Request, allowed: set[str]) -> None:
    """An unknown query parameter is an error. A silent ignore would let a random parameter defeat the cache."""
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ApiError(400, f"Unknown parameter: {unknown[0]}", parameter=unknown[0])


def respond(request: Request, model: type[Model], payload: dict, row_order: list[str] | None = None) -> Response:
    """Check the payload against its response model, then send it. A payload the model rejects is a server fault, never a changed response."""
    body = model.model_validate({**payload, "source": source(request)}).model_dump(mode="json", exclude_unset=True)
    if row_order:                                                # a caller who picks fields gets them in the order asked for
        body["data"] = [{f: row[f] for f in row_order if f in row} for row in body["data"]]
    return caching.conditional(request, json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(), "application/json")


def make_cursor(request: Request, route: str, values: list) -> str:
    app = request.app.state
    return cursors.encode(app.settings.cursor_secret, app.db.data_version, route, values)


def read_cursor(request: Request, route: str, cursor: str, shape: tuple) -> list:
    """The checked key values of a cursor (see cursors.py), or a plain 400. `shape` is required: there is no way to read a cursor unchecked."""
    app = request.app.state
    try:
        return cursors.decode(app.settings.cursor_secret, app.db.data_version, route, cursor, shape)
    except ValueError as error:
        raise ApiError(400, str(error), parameter="cursor") from None


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
