"""Errors as RFC 9457 problem details. They never carry a stack trace, a path, SQL or a library name."""
from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM = "application/problem+json"
TITLES = {400: "Bad request", 401: "Unauthorised", 404: "Not found", 405: "Method not allowed", 413: "Request too large", 422: "Invalid parameter",
          429: "Too many requests", 500: "Server error", 503: "Busy"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None, parameter: str | None = None):
        self.status, self.detail, self.headers, self.parameter = status, detail, headers or {}, parameter


def problem(request: Request, status: int, detail: str, headers: dict[str, str] | None = None, parameter: str | None = None) -> JSONResponse:
    body = {"type": "about:blank", "title": TITLES.get(status, "Error"), "status": status, "detail": detail[:300],
            "instance": request.url.path, "request_id": getattr(request.state, "request_id", None)}
    if parameter:
        body["parameter"] = parameter
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers={"Cache-Control": "no-store", **(headers or {})})
