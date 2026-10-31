"""The icons a browser asks for by name at the root of the host: /favicon.ico and the touch icons.

They are the same CricAtlas files the documentation page links, served here whether or not the documentation page is enabled. A browser that opens a JSON or error answer has no page to read an icon link from, so
it asks for /favicon.ico, and without these routes it got a 404 and could show an icon remembered from another address. Not in the OpenAPI document.
"""
from pathlib import Path

from fastapi import APIRouter, Request

from cricket_api.routes.docs import ASSETS, respond

router = APIRouter()

# The path, the file in the documentation's assets and its type. /favicon.ico carries the 32 px PNG: browsers read an icon by its bytes, and no new artwork or file format is made for it.
ROOT_ICONS = {
    "/favicon.ico": ("icon-32.png", "image/png"),
    "/favicon.svg": ("brand-mark.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def load(assets: Path = ASSETS) -> dict[str, bytes]:
    return {path: (assets / name).read_bytes() for path, (name, _) in ROOT_ICONS.items()}


def icon_route(path: str, media_type: str):
    async def icon(request: Request):
        return respond(request, request.app.state.root_icons[path], media_type)
    router.add_api_route(path, icon, methods=["GET"], include_in_schema=False, name=f"root_icon_{path.strip('/').replace('.', '_').replace('-', '_')}")


for _path, (_, _type) in ROOT_ICONS.items():
    icon_route(_path, _type)
