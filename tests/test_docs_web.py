"""The web variant of the documentation: the same page and OpenAPI document for the web app's /api, served under /_via/web so one URL never has two answers. Its links, share URLs and server address carry the /api
prefix and the web app's origin. The API's own /docs and /openapi.json are not changed by it."""
import json
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from cricket_api.app import create_app
from cricket_api.settings import ConfigError, Settings

WEB = "https://app.example.test"
SECRET = "s3cr3t-signing-value-0123456789abcdef"


class Metas(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metas, self.links = {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("property") or a.get("name", "")).startswith(("og:", "twitter:")):
            self.metas[a.get("property") or a["name"]] = a["content"]
        if tag == "link" and a.get("href"):
            self.links.append(a["href"])


def parse(text):
    p = Metas()
    p.feed(text)
    return p


def make(dataset, **kw):
    return TestClient(create_app(Settings(data_dir=dataset, cursor_secret=SECRET, **kw)), base_url="http://testserver")


@pytest.fixture
def web(dataset):
    client = make(dataset, web_origin=WEB, public_url="https://api.example.test")
    yield client
    client.app.state.db.close()


def test_the_web_page_links_its_assets_under_the_prefix_and_names_the_web_origin_in_its_share_tags(web):
    page = parse(web.get("/_via/web/docs").text)
    assets = [h for h in page.links if "/docs/assets/" in h]
    assert assets and all(h.startswith("/api/docs/assets/") for h in assets), assets
    assert page.metas["og:image"] == page.metas["twitter:image"] == f"{WEB}/api/docs/assets/og-api-docs.png"
    assert page.metas["og:url"] == f"{WEB}/api/docs" and page.metas["twitter:card"] == "summary_large_image"


def test_the_apis_own_page_is_unchanged_and_names_its_own_origin(web):
    text = web.get("/docs").text
    page = parse(text)
    assert all(h.startswith("/docs/assets/") for h in page.links if "/docs/assets/" in h) and "/api/docs" not in text
    assert page.metas["og:url"] == "https://api.example.test/docs" and page.metas["og:image"] == "https://api.example.test/docs/assets/og-api-docs.png"


def test_the_two_pages_differ_only_by_the_address_they_are_read_at(web):
    def normal(text):
        text = re.sub(r"<p>(?:The address of this server is|Add the address of this server).*?</p>", "<p>ADDRESS</p>", text, count=1)
        text = text.replace("/api/docs", "/docs").replace("<code>/api/openapi.json</code>", "<code>/openapi.json</code>")
        return re.sub(r"https://(?:api|app)\.example\.test", "ORIGIN", text)
    assert normal(web.get("/docs").text) == normal(web.get("/_via/web/docs").text)


def test_the_quickstart_names_the_address_on_the_web_page_only(web):
    via, own = web.get("/_via/web/docs").text, web.get("/docs").text
    assert f"The address of this server is" in via and f"curl &quot;{WEB}/api/v1/players?q=Stokes&quot;" in via.replace('"', "&quot;") or f'curl "{WEB}/api/v1/players?q=Stokes"' in via
    assert "$API/v1/players" in own and "The address of this server is" not in own


def test_the_reference_says_where_the_openapi_document_is_on_each_page(web):
    assert "document at <code>/api/openapi.json</code>" in web.get("/_via/web/docs").text
    assert "document at <code>/openapi.json</code>" in web.get("/docs").text


def test_assets_are_served_under_one_route_and_only_the_real_names_answer(web):
    css = web.get("/_via/web/docs/assets/docs.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and css.content == web.get("/docs/assets/docs.css").content
    for name, kind in (("og-api-docs.png", "image/png"), ("brand-mark.svg", "image/svg+xml"), ("outfit-latin.woff2", "font/woff2"), ("icon-32.png", "image/png"), ("apple-touch-icon.png", "image/png")):
        r = web.get(f"/_via/web/docs/assets/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == kind and r.content == web.get(f"/docs/assets/{name}").content, name
    for bad in ("nothing.css", "..%2Fsecret", "docs.css%00", "DOCS.CSS", "docs.css/x", ".env", "docs"):
        assert web.get(f"/_via/web/docs/assets/{bad}").status_code == 404, bad


def test_the_web_openapi_document_names_the_web_address_as_its_server_and_otherwise_matches(web):
    own, via = web.get("/openapi.json").json(), web.get("/_via/web/openapi.json").json()
    assert via["servers"] == [{"url": f"{WEB}/api"}] and "servers" not in own
    via.pop("servers")
    assert via == own, "same routes, same schemas, same data fields"


def test_each_variant_has_its_own_validator_and_the_same_policy(web):
    a, b = web.get("/docs"), web.get("/_via/web/docs")
    assert a.headers["etag"] != b.headers["etag"]
    assert a.headers["content-security-policy"] == b.headers["content-security-policy"] and a.headers["cache-control"] == b.headers["cache-control"]
    assert web.get("/_via/web/docs", headers={"If-None-Match": b.headers["etag"]}).status_code == 304
    assert web.get("/docs", headers={"If-None-Match": b.headers["etag"]}).status_code == 200, "one variant's validator never confirms the other's page"


def test_without_a_web_origin_the_page_and_document_use_a_relative_prefix_and_no_share_tags(dataset):
    client = make(dataset)
    try:
        page = parse(client.get("/_via/web/docs").text)
        assert page.metas == {} and all(h.startswith("/api/docs/assets/") for h in page.links if "/docs/assets/" in h)
        assert client.get("/_via/web/openapi.json").json()["servers"] == [{"url": "/api"}]
    finally:
        client.app.state.db.close()


def test_the_data_set_is_named_the_same_on_both_pages(web):
    name = web.get("/freshness").json()["coverage"]["dataset_display_name"]
    assert name in web.get("/docs").text and name in web.get("/_via/web/docs").text


@pytest.mark.parametrize("bad", ["http://app.example.test", "https://app.example.test/", "https://app.example.test/api", "app.example.test", "https://", "https://A.example.test"])
def test_the_web_origin_is_an_https_origin_and_nothing_else(dataset, bad):
    with pytest.raises(ConfigError):
        Settings(data_dir=dataset, cursor_secret=SECRET, web_origin=bad)


def test_the_web_routes_are_not_in_the_openapi_document_or_the_reference(web):
    spec = web.get("/openapi.json").json()
    assert not any(path.startswith("/_via") for path in spec["paths"]) and "/_via" not in web.get("/docs").text
