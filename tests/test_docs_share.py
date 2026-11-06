"""The documentation page's link-preview tags and share image: absolute URLs from the configured public origin only, no tag without one, and the image is served under the page's own policy."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricket_api.app import create_app
from cricket_api.settings import ConfigError, Settings

ORIGIN = "https://api.example.test"
ASSETS = Path(__file__).resolve().parents[1] / "cricket_api" / "docsite" / "assets"


class Metas(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("property") or a.get("name", "")).startswith(("og:", "twitter:")):
            self.found[a.get("property") or a["name"]] = a["content"]


def metas(text):
    parser = Metas()
    parser.feed(text)
    return parser.found


@pytest.fixture
def with_origin(dataset):
    client = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef", public_url=ORIGIN)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


def test_the_page_names_its_share_image_with_an_absolute_url_and_the_card_type(with_origin):
    found = metas(with_origin.get("/docs").text)
    assert found["og:image"] == found["twitter:image"] == f"{ORIGIN}/docs/assets/og-api-docs.png"
    assert found["twitter:card"] == "summary_large_image" and found["og:type"] == "website" and found["og:url"] == f"{ORIGIN}/docs"
    assert (found["og:image:width"], found["og:image:height"]) == ("1200", "630") and found["og:title"] == "CricAtlas API Documentation" and found["og:image:alt"]


def test_the_image_is_the_committed_banner_served_with_the_pages_policy_and_no_loosening(with_origin):
    r = with_origin.get("/docs/assets/og-api-docs.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == (ASSETS / "og-api-docs.png").read_bytes()
    assert int.from_bytes(r.content[16:20], "big") == 1200 and int.from_bytes(r.content[20:24], "big") == 630
    policy = r.headers["content-security-policy"]
    assert "img-src 'self'" in policy and "default-src 'none'" in policy and "http" not in policy and "data:" not in policy
    assert r.headers["cache-control"] == "public, max-age=3600" and with_origin.get("/docs/assets/og-api-docs.png", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_no_share_tags_without_a_configured_origin(client):
    assert metas(client.get("/docs").text) == {}, "a relative image address is not accepted by preview sites, so none is written"


@pytest.mark.parametrize("bad", ["http://api.example.test", "https://api.example.test/", "https://api.example.test/docs", "api.example.test", "https://", "https://a b.test", "javascript:alert(1)"])
def test_the_public_origin_is_an_https_origin_and_nothing_else(dataset, bad):
    with pytest.raises(ConfigError):
        Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef", public_url=bad)


def test_the_page_policy_and_other_head_content_are_unchanged_by_the_tags(with_origin):
    r = with_origin.get("/docs")
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    assert not re.search(r"<script", r.text)


def test_the_origin_the_compose_file_builds_from_the_host_name_gives_the_page_its_share_urls(dataset, monkeypatch):
    """The container gets CRICKET_API_PUBLIC_URL=https://<API_HOSTNAME> from compose and nothing else. Read through the real settings path (the environment), /docs names the image by that absolute URL."""
    monkeypatch.setenv("CRICKET_API_PUBLIC_URL", "https://api-host.example.test")
    client = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef")), base_url="http://testserver")
    try:
        found = metas(client.get("/docs").text)
        assert found["og:image"] == found["twitter:image"] == "https://api-host.example.test/docs/assets/og-api-docs.png"
        assert found["og:url"] == "https://api-host.example.test/docs" and found["twitter:card"] == "summary_large_image"
        assert client.get("/docs/assets/og-api-docs.png").headers["content-type"] == "image/png", "the URL the tags name is served by the API itself"
    finally:
        client.app.state.db.close()


def test_an_empty_origin_in_the_environment_writes_no_tags(dataset, monkeypatch):
    monkeypatch.setenv("CRICKET_API_PUBLIC_URL", "")
    client = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef")), base_url="http://testserver")
    try:
        assert metas(client.get("/docs").text) == {}
    finally:
        client.app.state.db.close()
