"""The banner at the top of the README: an SVG drawn once at start from the live data, served beside the documentation's other assets. It must show what the request it names returns, carry everything it needs
(GitHub shows no outside address), never run a script, and not stop the API from starting if it cannot be drawn."""
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from cricket_api import docsite
from cricket_api.app import create_app
from cricket_api.docsite import banner
from cricket_api.settings import Settings

PATH = "/docs/assets/banner.svg"
SVG = "{http://www.w3.org/2000/svg}"


def texts(root):
    return ["".join(t.itertext()) for t in root.iter(SVG + "text")]


def test_the_banner_is_served_as_an_svg_with_its_own_narrow_policy_and_the_assets_caching(client):
    r = client.get(PATH)
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
    policy = r.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "script" not in policy and "http" not in policy and "sandbox" in policy
    assert r.headers["cache-control"] == "public, max-age=3600" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get(PATH, headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_the_web_variant_serves_the_same_bytes(client):
    assert client.get("/_via/web" + PATH).content == client.get(PATH).content


def test_the_banner_shows_what_the_request_it_names_returns(client):
    root = ET.fromstring(client.get(PATH).content)
    lines = texts(root)
    request = "GET " + banner.ROUTE + "?" + "&".join(f"{k}={v}" for k, v in banner.QUERY)
    assert request in lines, "the request line is drawn from the same constants the route is asked with"
    answer = client.get(banner.ROUTE, params=dict(banner.QUERY)).json()["data"]["leaders"]
    assert answer, "the test data has IPL matches"
    drawn = " ".join(lines)
    for leader in answer:
        assert f'"name": "{leader["name"]}"' in drawn or f'"{leader["name"]}"' in drawn
        assert str(leader["value"]) in drawn
    order = [drawn.index(f'"{leader["name"]}"') for leader in answer]
    assert order == sorted(order), "the leaders are drawn in the order the route returns them"


def test_the_file_is_well_formed_and_brings_everything_it_needs(client):
    body = client.get(PATH).content
    root = ET.fromstring(body)
    assert root.tag == SVG + "svg" and root.get("viewBox") == "0 0 1280 640"
    text = body.decode()
    assert "<script" not in text and "foreignObject" not in text and "<image" not in text
    assert not re.search(r"(?:href|src)=[\"'](?!data:|#)", text), "nothing refers to another address"
    assert not re.search(r"url\((?!data:|#|[\"']?data:)", text), "no style refers to another address"
    assert "@font-face" not in text and "Outfit" not in text, "GitHub's image proxy blocks fonts inside a file, so the Outfit text is drawn as outlines"
    assert text.count("<path") >= 6 and re.findall(r"font-family:([^;}]+)", text) == [banner.MONO] * 2
    assert len(body) < 200_000


def test_a_name_with_markup_is_text_and_never_an_element(dataset, monkeypatch):
    nasty = [{"rank": 1, "name": '<script>alert(1)</script> & "q" \'s\'', "value": 7}]
    monkeypatch.setattr(banner, "leaders", lambda db: nasty)
    app = create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef"))
    try:
        body = TestClient(app).get(PATH).content
        root = ET.fromstring(body)
        assert not list(root.iter(SVG + "script")) and b"<script>" not in body
        assert any("<script>alert(1)</script>" in t for t in texts(root))
    finally:
        app.state.db.close()


def test_no_leaders_still_draws_a_valid_banner(dataset, monkeypatch):
    monkeypatch.setattr(banner, "leaders", lambda db: [])
    app = create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef"))
    try:
        r = TestClient(app).get(PATH)
        assert r.status_code == 200 and ET.fromstring(r.content).tag == SVG + "svg"
    finally:
        app.state.db.close()


def test_a_banner_that_cannot_be_drawn_does_not_stop_the_api(dataset, monkeypatch, caplog):
    def broken(db, assets):
        raise RuntimeError("no banner today")
    monkeypatch.setattr(banner, "build", broken)
    app = create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef"))
    try:
        c = TestClient(app)
        assert c.get("/health").status_code == 200 and c.get("/docs").status_code == 200
        assert c.get(PATH).status_code == 404 and c.get("/_via/web" + PATH).status_code == 404
        assert "the README banner could not be drawn" in caplog.text
    finally:
        app.state.db.close()


def test_the_banner_is_a_documentation_asset_with_no_cost_class_gap():
    from cricket_api import admission
    assert admission.TABLE[PATH] == admission.NONE and docsite is not None
