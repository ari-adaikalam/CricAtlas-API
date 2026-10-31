"""The icons a browser asks for at the root of the host: served from the CricAtlas files, with or without the documentation page, and not part of the OpenAPI document."""
from pathlib import Path

import pytest

from cricket_api.routes import icons

ASSETS = Path(__file__).resolve().parents[1] / "cricket_api" / "docsite" / "assets"


@pytest.mark.parametrize("path, name, media", [(p, n, m) for p, (n, m) in icons.ROOT_ICONS.items()])
def test_each_root_icon_is_the_brand_file_with_the_right_type_and_a_cache_policy(client, path, name, media):
    r = client.get(path)
    assert r.status_code == 200 and r.content == (ASSETS / name).read_bytes()
    assert r.headers["content-type"].startswith(media) and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "public, max-age=3600" and r.headers["content-security-policy"].startswith("default-src \x27none\x27")
    assert client.get(path, headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_the_icons_are_real_images_of_the_stated_size(client):
    small, touch = client.get("/favicon.ico").content, client.get("/apple-touch-icon.png").content
    assert small[:8] == touch[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(small[16:20], "big") == 32 and int.from_bytes(touch[16:20], "big") == 180
    assert b"<script" not in client.get("/favicon.svg").content


def test_the_icons_do_not_depend_on_the_documentation_page_and_stay_out_of_openapi(make_client):
    off = make_client(docs_enabled=False)
    assert off.get("/docs").status_code == 404
    assert off.get("/favicon.ico").status_code == 200 and off.get("/apple-touch-icon.png").status_code == 200
    schema = off.get("/openapi.json").json()
    assert not [p for p in schema["paths"] if "icon" in p]


def test_other_icon_names_and_methods_are_still_refused(client):
    assert client.get("/favicon.png").status_code == 404 and client.get("/favicon.ico/x").status_code == 404
    assert client.post("/favicon.ico").status_code == 405
    assert client.get("/favicon.ico?x=1").status_code == 200, "a query string changes nothing"


def test_the_icons_are_in_the_built_package_and_the_same_files_as_the_documentation_links(client):
    for path, (name, _) in icons.ROOT_ICONS.items():
        assert (ASSETS / name).is_file()
    assert client.get("/favicon.ico").content == client.get("/docs/assets/icon-32.png").content
