"""The error responses the OpenAPI document lists, and the pass that gives them their media type.

FastAPI writes an error response as application/json and adds its own 422 shape. Both are wrong here: every error is
problem details (application/problem+json) with the Problem model, including the 422.
"""
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from cricket_api.errors import PROBLEM, TITLES
from cricket_api.models import Problem

BASE = (400, 422, 429, 500, 503)
# What a filter means, given once. A route that already describes a parameter keeps its own words.
FILTER_NOTES = {
    "date_from": "Include matches that started on or after this date. A match is selected whole by its start date, not by the days it was played on.",
    "date_to": "Include matches that started on or before this date. A match is selected whole by its start date, so a Test that starts on this date brings in all of its days.",
    "season": "Include only this season, as Cricsheet labels it, such as 2019 or 2019/20.",
    "collection": "Include only this competition, by its Cricsheet collection code.",
    "format": "Include only this format of the game.",
}
PHRASES = {400: "An unknown parameter or a bad value", 404: "No such match or innings", 422: "A parameter that failed validation", 429: "Too many requests, see Retry-After",
           500: "A server fault", 503: "Busy, see Retry-After"}


def errors(*extra: int) -> dict:
    """The error responses of a route, as FastAPI's `responses` argument."""
    return {code: {"model": Problem, "description": PHRASES.get(code, TITLES[code])} for code in (*BASE, *extra)}


def build(app: FastAPI) -> dict:
    schema = get_openapi(title=app.title, version=app.version, description=app.description, license_info=app.license_info, contact=app.contact, routes=app.routes)
    for path in schema["paths"].values():
        for operation in path.values():
            for code, response in operation["responses"].items():
                if code.startswith(("4", "5")) and "content" in response:
                    response["content"] = {PROBLEM: response["content"].pop("application/json")}
    for path in schema["paths"].values():
        for operation in path.values():
            for parameter in operation.get("parameters", []):
                if parameter["name"] in FILTER_NOTES and not parameter.get("description"):
                    parameter["description"] = FILTER_NOTES[parameter["name"]]
    schema["components"]["schemas"].pop("HTTPValidationError", None)
    schema["components"]["schemas"].pop("ValidationError", None)
    return schema


def install(app: FastAPI) -> None:
    def openapi() -> dict:
        if not app.openapi_schema:
            app.openapi_schema = build(app)
        return app.openapi_schema
    app.openapi = openapi
