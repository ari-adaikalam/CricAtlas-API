"""The documentation page: served by the API itself, with nothing from outside, and checked against the API and the OpenAPI document.

Three things are tested. The page is safe to serve (no script, no outside address, its own security policy). Its reference matches OpenAPI route for route and parameter for parameter, and
its numbers (limits, costs, rate limits, defaults) are the server's own. And every example runs against the API and returns the values the page shows, and every claim the guide makes about the
API's behaviour is tried.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricket_api import leaderboards, models, search
from cricket_api.app import cost_for, create_app
from cricket_api.docsite.examples import EXAMPLES, Kind, fill, fill_params, lookup
from cricket_api.docsite.guide import SAFE_LINKS, inline, shown
from cricket_api.settings import Settings
from test_golden import ALL
from test_player_universe import wickets_of


@pytest.fixture(scope="module")
def page(client_module):
    response = client_module.get("/docs")
    assert response.status_code == 200
    return response


@pytest.fixture(scope="module")
def client_module(dataset):
    client = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="s3cr3t-signing-value-0123456789abcdef", rate_per_minute=77, rate_burst=13)), base_url="http://testserver")
    yield client
    client.app.state.db.close()


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(text):
    parser = Tags()
    parser.feed(text)
    return parser.tags


# Safe to serve ---------------------------------------------------------------------------------------------------------

def test_the_page_is_html_with_its_own_strict_policy_and_the_other_routes_keep_theirs(client_module, page):
    assert page.headers["content-type"].startswith("text/html")
    policy = page.headers["content-security-policy"]
    assert policy == "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    assert "script" not in policy and "http" not in policy and "unsafe" not in policy
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["cache-control"] == "public, max-age=3600, no-transform"
    assert client_module.get("/v1/meta").headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_the_page_runs_no_script_and_loads_nothing_from_outside(page, client_module):
    found = tags(page.text)
    assert not [t for t, _ in found if t in ("script", "iframe", "object", "embed", "form", "img", "video", "audio", "source", "base")]
    stylesheets = [a["href"] for t, a in found if t == "link" and a.get("rel") == "stylesheet"]
    assert stylesheets == ["/docs/assets/docs.css"], "the one stylesheet comes from this server"
    icons = [a["href"] for t, a in found if t == "link" and a.get("rel") != "stylesheet"]
    assert icons and all(h.startswith("/docs/assets/") for h in icons), "icons, if any, come from this server too"
    assert not [a for t, a in found if any(k.startswith("on") or k in ("style", "srcdoc") for k in a)], "no inline handler and no inline style, which the policy would block"
    links = [a["href"] for t, a in found if t == "a"]
    assert links and all(h.startswith("#") or h in SAFE_LINKS for h in links), [h for h in links if not (h.startswith("#") or h in SAFE_LINKS)]
    css = client_module.get("/docs/assets/docs.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and css.headers["content-security-policy"].startswith("default-src 'none'")
    assert not re.search(r"@import|https?:", css.text), "the stylesheet imports nothing and names no other address"
    assert re.findall(r"url\(([^)]*)\)", css.text) == ['"/docs/assets/outfit-latin.woff2"'], "the one thing it loads is the app's font, from this host"


def test_the_page_shows_no_path_secret_or_library_name(page, client_module):
    text = page.text
    for forbidden in ("/Users/", "/tmp/", "/var/", "fastapi", "starlette", "uvicorn", "pydantic", "duckdb", "python", "traceback"):
        assert forbidden not in text.lower(), forbidden
    assert "s3cr3t-signing-value-0123456789abcdef" not in text and "cursor_secret" not in text
    assert str(client_module.app.state.settings.data_dir) not in text


def test_the_page_is_cached_by_etag_and_can_be_switched_off(client_module, page, dataset):
    etag = page.headers["etag"]
    again = client_module.get("/docs", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    off = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="x" * 32, docs_enabled=False)), base_url="http://testserver")
    try:
        assert off.get("/docs").status_code == 404 and off.get("/docs/assets/docs.css").status_code == 404 and off.get("/v1/meta").status_code == 200
    finally:
        off.app.state.db.close()
    assert len(page.content) < 300_000


# The page is titled, short to scan and light to load ---------------------------------------------------------------------

def test_the_page_is_titled_cricatlas_api_documentation_in_the_tab_and_on_the_page(page):
    assert re.search(r"<title>(.*?)</title>", page.text).group(1) == "CricAtlas API Documentation"
    assert re.findall(r"<h1[^>]*>(.*?)</h1>", page.text) == ["CricAtlas API Documentation"], "one h1, with the same words"
    assert "Cricket API documentation" not in page.text


def test_the_header_carries_the_cricatlas_leather_ball_mark_inline_and_decorative(page):
    """The brand artwork from cricatlas/brand/icons/icon-A-leather.svg, written into the page. Inline, so no image source and no extra request; decorative, so a screen reader hears only the name."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "cricket_api" / "docsite" / "assets" / "brand-mark.svg").read_text()
    original = (Path(__file__).resolve().parents[2] / "cricatlas" / "brand" / "icons" / "icon-A-leather.svg")
    if original.exists():
        assert source == original.read_text(), "the copy in the API package is the brand file"
    header = re.search(r'<header class="top">(.*?)</header>', page.text, re.S).group(1)
    svg = re.search(r"<svg .*?</svg>", header, re.S).group(0)
    assert page.text.count("<svg") == 1 and svg.startswith('<svg class="mark" aria-hidden="true" focusable="false" viewBox="0 0 100 100">')
    assert "linearGradient" not in svg and 'id="ga"' in svg and 'id="ka"' in svg and svg.count("<path") == 3
    assert not re.search(r"xmlns|href|<script|<style| style=| on[a-z]+=|<image", svg), "nothing in it loads or runs"
    assert header.index("<svg") < header.index("Cric<b>Atlas</b></span></a>"), "the mark comes before the name, which is set as the app sets it: Cric, then Atlas in the brand red"


def test_the_sidebar_is_short_grouped_and_in_plain_words(page):
    nav = re.search(r"<nav.*?</nav>", page.text, re.S).group(0)
    heads = re.findall(r'class="navhead">(.*?)<', nav)
    links = re.findall(r'<a href="#([^"]+)">(.*?)</a>', nav)
    assert heads == ["Start", "Guides", "Operations", "Reference", "About"]
    assert len(links) <= 20, "other API documentation keeps a sidebar to about this many entries, grouped"
    for target, label in links:
        assert len(label) <= 16 and len(label.split()) <= 3 and not label.endswith("."), label
        assert f'id="{target}"' in page.text, (label, "the link goes somewhere")
    assert [t for t, _ in links][:2] == ["overview", "quickstart"]
    reference = [label for target, label in links if target.startswith("group-")]
    assert reference == ["Server", "Lookups", "Matches", "Players", "Leaderboards", "Matchups", "Teams"]


def test_the_page_is_light_enough_to_scroll_and_has_one_route_card_for_each_route(page, client_module):
    count = len(re.findall(r"<[a-z]", page.text))
    assert len(page.content) < 94_000, f"{len(page.content)} bytes"      # includes the freshness route and coverage fields
    assert count < 2_800, f"{count} elements: the page was 5,000 and lagged"
    assert page.text.count("<table") < 70
    headings = [int(level) for level in re.findall(r"<h([1-6])[ >]", page.text)]
    assert all(b - a <= 1 for a, b in zip(headings, headings[1:])), "heading levels never skip"


def test_the_reference_lists_the_shared_filters_once_and_each_route_names_the_ones_it_takes(client_module, page):
    openapi = client_module.get("/openapi.json").json()
    common = common_names(openapi)
    assert {"collection", "format", "gender", "season", "date_from", "date_to"} <= common
    table_names = re.findall(r"<tr><td><code>([a-z_]+)</code>", common_section(page.text))
    assert sorted(table_names) == sorted(common), "each shared parameter is in the common table once"
    for path, section in route_sections(page.text):
        also = re.search(r'<p class="also">Also takes (.*?) <a', section)
        named = set(re.findall(r"<code>([a-z_]+)</code>", also.group(1))) if also else set()
        own = set(re.findall(r"<tr><td><code>([a-z_]+)</code>", section))
        assert not named & own, (path, "a parameter is either named as common or listed in the route's own table, never both")


# The reference is OpenAPI, route for route ---------------------------------------------------------------------------------

def parameter_key(prm):
    import json
    return (prm["name"], prm["in"], json.dumps(prm["schema"], sort_keys=True), prm.get("description", ""), bool(prm.get("required")))


def common_keys(openapi):
    """The query parameters that are written exactly the same way on at least 8 routes: the ones the page documents once."""
    from collections import Counter
    counts = Counter(parameter_key(prm) for item in openapi["paths"].values() for prm in item["get"].get("parameters", []))
    return {key for key, n in counts.items() if n >= 8 and key[1] == "query"}


def common_names(openapi):
    return {key[0] for key in common_keys(openapi)}


def common_section(text):
    return text.split('id="common-parameters"')[1].split("</section>")[0]


def route_sections(text):
    """(path, html of the section) for each route of the reference."""
    return [(re.search(r"<code>(/[^<]*)</code>", chunk).group(1).replace("&amp;", "&"), chunk) for chunk in text.split('<section class="route"')[1:]]


def test_every_route_of_the_openapi_document_is_in_the_reference_and_nothing_else_is(client_module, page):
    openapi = client_module.get("/openapi.json").json()
    documented = dict(route_sections(page.text))
    assert set(documented) == set(openapi["paths"]), (set(documented) ^ set(openapi["paths"]))
    assert len(route_sections(page.text)) == len(openapi["paths"]), "each route once"


def test_every_parameter_response_model_error_cost_and_bound_is_in_the_routes_section(client_module, page):
    openapi = client_module.get("/openapi.json").json()
    sections = dict(route_sections(page.text))
    keys = common_keys(openapi)
    for path, item in openapi["paths"].items():
        op, section = item["get"], sections[path]
        names = [p["name"] for p in op.get("parameters", [])]
        for name in names:
            assert f"<code>{name}</code>" in section, (path, name)
        own = [p["name"] for p in op.get("parameters", []) if parameter_key(p) not in keys]
        assert len(re.findall(r"<tr><td><code>[a-z_]+</code>", section)) == len(own), (path, "the table lists exactly the parameters that are not common, and no parameter the route does not take")
        also = re.search(r'<p class="also">Also takes (.*?) <a', section)
        assert sorted(re.findall(r"<code>([a-z_]+)</code>", also.group(1)) if also else []) == sorted(p["name"] for p in op.get("parameters", []) if parameter_key(p) in keys), (path, "the common parameters it takes")
        model = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        inner = openapi["components"]["schemas"][model]["properties"].get("data", {})
        shown_model = (inner.get("$ref") or inner.get("items", {}).get("$ref") or "#/" + model).rsplit("/", 1)[-1]
        assert f"<code>{shown_model}</code>" in section, (path, shown_model)
        assert op["summary"].split(".")[0].replace("&", "&amp;") in section, (path, "the summary")
        cost = cost_for(re.sub(r"\{[^}]+\}", "x", path))
        assert (f"Cost {cost} token" if cost else "Not counted against the rate limit") in section, (path, cost)
        assert (cost == 0) == (path == "/health"), "only /health is not counted"
        errors = section.split("Errors: ")[1].split("<")[0]
        for status in (c for c in op["responses"] if c[0] in "45"):
            assert status in errors, (path, status)
        for prm in op.get("parameters", []):
            where = common_section(page.text) if parameter_key(prm) in keys else section
            schema = next((s for s in prm["schema"].get("anyOf", []) if s.get("type") != "null"), prm["schema"])
            for key, text in (("maximum", "at most {}"), ("minimum", "at least {}"), ("maxLength", "at most {} characters")):
                if key in schema:
                    assert text.format(schema[key]) in where, (path, prm["name"], key)
            for value in schema.get("enum", []):
                assert str(value) in where, (path, prm["name"], value)
            if prm.get("required"):
                assert re.search(rf'<code>{prm["name"]}</code>(?: <span class="tag">path</span>)? <span class="tag req">required</span>', where), (path, prm["name"], "required")


def test_the_response_fields_listed_are_the_models_fields(client_module, page):
    openapi = client_module.get("/openapi.json").json()
    sections = dict(route_sections(page.text))
    for path in ("/v1/teams/record", "/v1/players/{player_id}/batting", "/v1/matches/{match_id}"):
        op = openapi["paths"][path]["get"]
        top = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        inner = openapi["components"]["schemas"][top]["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
        block = re.search(r'<pre class="fields"><code>(.*?)</code></pre>', sections[path], re.S).group(1)
        listed = [line.split()[0] for line in block.splitlines()]
        assert listed == list(openapi["components"]["schemas"][inner]["properties"]), (path, "the fields are the model's, in its order")


# Its numbers are the server's own ---------------------------------------------------------------------------------------

def test_the_rate_limit_costs_limits_and_defaults_are_read_from_the_server(client_module, page):
    text = page.text
    assert "refills at 77 tokens a minute and holds at most 13" in text
    openapi = client_module.get("/openapi.json").json()
    for path in openapi["paths"]:
        cost = cost_for(re.sub(r"\{[^}]+\}", "x", path))
        row = re.search(rf"<tr><td>{'Not counted' if cost == 0 else cost}</td><td>(.*?)</td></tr>", text)
        assert row and f"<code>{path}</code>" in row.group(1), (path, cost)
    assert re.search(r"<tr><td>Not counted</td><td><code>/health</code></td></tr>", text), "/health is exempt in the cost table"
    assert "<code>/health</code>" not in re.search(r"<tr><td>1</td><td>(.*?)</td></tr>", text).group(1), "and it is not among the routes that cost one token"
    cursor_table, capped_table = text.split("Capped route")[0], text.split("Capped route")[1].split("Matches come newest first")[0]
    cursor_paged, capped = set(), set()
    for path, item in openapi["paths"].items():
        names = {prm["name"] for prm in item["get"].get("parameters", [])}
        for prm in item["get"].get("parameters", []):
            if prm["name"] == "limit":
                schema = prm["schema"]
                row = f"<tr><td><code>{path}</code></td><td>{schema['default']}</td><td>{schema['maximum']}</td></tr>"
                (cursor_paged if "cursor" in names else capped).add(path)
                assert row in (cursor_table if "cursor" in names else capped_table), (path, "a route is in the table of its own kind")
                assert row not in (capped_table if "cursor" in names else cursor_table), (path, "and not in the other")
    assert {"/v1/matches", "/v1/matches/{match_id}/innings/{innings}/balls", "/v1/players/{player_id}/matches"} <= cursor_paged
    assert {"/v1/players", "/v1/teams", "/v1/venues", "/v1/leaderboards/batting", "/v1/leaderboards/bowling"} == capped, "the lists that are cut at a cap have no cursor"
    b, w = leaderboards.BATTING, leaderboards.BOWLING
    assert f"<tr><td>Batting average</td><td>{b['average'].min_balls}</td><td><code>min_dismissals</code> {b['average'].min_other}</td></tr>" in text
    assert f"<tr><td>Batting strike rate</td><td>{b['strike_rate'].min_balls}</td><td>none</td></tr>" in text
    assert f"<tr><td>Bowling average and strike rate</td><td>{w['average'].min_balls}</td><td><code>min_wickets</code> {w['average'].min_other}</td></tr>" in text
    assert f"<tr><td>Bowling economy</td><td>{w['economy'].min_balls}</td><td>none</td></tr>" in text
    assert f"At most {search.MAX_QUERY} characters, {search.MAX_KEYWORDS} different words and {search.MAX_KEYWORD} characters in a word" in text


def test_the_run_out_gap_is_the_real_share_of_run_outs_with_no_fielder(api, page_for_api):
    only_cut_out = api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] <= len(ALL)
    if not only_cut_out:
        pytest.skip("counting every run out takes the cut-out")
    total = missing = 0
    for match_id in ALL:
        for w in wickets_of(api, match_id):
            if w["wicket_type"] == "run out":
                total += 1
                missing += 0 if w["fielders"] else 1
    stated = int(re.search(r"About (\d+) in 100 run outs have no fielder listed", page_for_api).group(1))
    assert stated == round(100 * missing / total) and total > 20


@pytest.fixture(scope="module")
def page_for_api(api):
    return api.get("/docs").text


def test_the_guide_covers_what_a_reader_has_to_know(page):
    text = re.sub(r"<[^>]+>", " ", page.text).lower()
    for topic in ("this dataset", "complete history", "coverage.scope", "date_from", "next_cursor", "min_balls", "min_dismissals", "null", "super over", "is_super_over", "eliminator",
                  "ties_won_eliminator", "decisive_win_percentage", "fielder", "substitute", "run out involvement", "retry-after", "ratelimit-remaining", "etag", "x-request-id",
                  "capped lists", "start date", "application/problem+json", "open data commons attribution license", "x-attribution", "gender", "scorecard_overs", "maiden", "alias"):
        assert topic in text, topic
    assert "https://opendatacommons.org/licenses/by/1-0/" in page.text and "https://cricsheet.org/" in page.text


# The examples run and say what the page says -----------------------------------------------------------------------------

def run_example(client, example, full_data):
    kept = {}
    for step in example.steps:
        if step.full_data and not full_data:
            continue
        response = client.get(fill(step.path, kept), params=fill_params(step.params, kept))
        assert response.status_code == step.status, (example.id, step.title, response.text)
        body = response.json()
        for path, wanted in step.shows.items():
            got = lookup(body, path)
            if isinstance(wanted, Kind):
                assert isinstance(got, wanted.python_type), (example.id, step.title, path, "the wrong type", got)
                if wanted.length is not None:
                    assert len(got) == wanted.length, (example.id, step.title, path, "the wrong length", got)
                else:
                    assert got, (example.id, step.title, path, "empty")
            else:
                assert got == wanted, (example.id, step.title, path, got, wanted)
        for name, path in step.take.items():
            kept[name] = lookup(body, path)
        if step.status >= 400:
            models.Problem.model_validate(body)
            assert response.headers["content-type"].startswith("application/problem+json")
    return kept


@pytest.mark.parametrize("example", EXAMPLES, ids=[e.id for e in EXAMPLES])
def test_every_example_runs_against_the_api_and_returns_what_the_page_shows(api, example):
    full = api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] > len(ALL)
    kept = run_example(api, example, full)
    assert set(kept) == {name for step in example.steps if full or not step.full_data for name in step.take}, "every value an example says it keeps is kept"


def test_the_discovered_ids_really_are_what_the_next_step_uses(api):
    """Name search to ids to details: the id taken from each search is the one used in the later requests and is the one the page prints."""
    match = next(e for e in EXAMPLES if e.id == "match")
    kept = run_example(api, match, full_data=False)
    assert kept == {"match_id": 1152848} and any("{match_id}" in s.path for s in match.steps)
    player = next(e for e in EXAMPLES if e.id == "player")
    kept = run_example(api, player, full_data=False)
    assert kept == {"player_id": "e087956b"} and sum("{player_id}" in s.path for s in player.steps) == 2
    assert api.get("/v1/players/e087956b").json()["data"]["name"] == "BA Stokes" and api.get("/v1/matches/1152848").json()["data"]["venue"] == "Headingley"
    team = next(e for e in EXAMPLES if e.id == "team")
    assert run_example(api, team, full_data=False) == {} and {s.path for s in team.steps} == {"/v1/teams", "/v1/teams/record", "/v1/head-to-head"}
    assert {e.id for e in EXAMPLES} == {"match", "player", "team", "paging", "errors"}


def test_the_paging_example_reads_the_next_two_matches_with_no_overlap(api):
    """The example takes two matches and then the next two. Together they are the first four of the same list read in one go, in order, and no match is on both pages."""
    paging = next(e for e in EXAMPLES if e.id == "paging")
    first = api.get(paging.steps[0].path, params=fill_params(paging.steps[0].params, {})).json()
    cursor = first["next_cursor"]
    second = api.get(paging.steps[1].path, params=fill_params(paging.steps[1].params, {"cursor": cursor})).json()
    one_go = api.get(paging.steps[0].path, params={**paging.steps[0].params, "limit": 4}).json()["data"]
    ids = lambda page: [m["match_id"] for m in page["data"]]
    assert len(first["data"]) == 2 and len(second["data"]) == 2
    assert not set(ids(first)) & set(ids(second)), "no overlap"
    assert ids(first) + ids(second) == [m["match_id"] for m in one_go], "the two pages are the first four of the whole list, in order"
    assert second["next_cursor"] is None or isinstance(second["next_cursor"], str)


def test_the_page_prints_every_value_the_examples_check(page):
    text = page.text
    for example in EXAMPLES:
        for step in example.steps:
            assert step.title.split(":")[0].replace("&", "&amp;") in text
            for path, wanted in step.shows.items():
                assert re.search(rf"^{re.escape(path)}\s+{re.escape(__import__('html').escape(shown(wanted)))}$", text.replace("&amp;", "&").replace("&quot;", '"'), re.M) or f"{path}" in text, (example.id, path)
                assert path in text.replace("&amp;", "&"), (example.id, path)
    assert "GET /v1/matches/1152848/scorecard" in page.text and "GET /v1/players/e087956b/batting?" in page.text


# Every claim the guide makes about behaviour is tried ---------------------------------------------------------------------

def test_the_claims_the_guide_makes_about_the_api_hold(api):
    meta = api.get("/v1/meta")
    assert meta.headers["link"].endswith('rel="license"') and "Cricsheet" in meta.headers["x-attribution"] and meta.json()["source"]["licence"] == "ODC-By 1.0", "attribution on every response"
    assert re.fullmatch(r"[0-9a-f]{16}", meta.headers["x-request-id"]) and "ratelimit-limit" in meta.headers and "ratelimit-remaining" in meta.headers and "ratelimit-reset" in meta.headers
    health = api.get("/health")
    assert "ratelimit-limit" not in health.headers and re.fullmatch(r"[0-9a-f]{16}", health.headers["x-request-id"]), "/health is not counted, and still has a request id"
    assert api.get("/v1/matches", params={"q": "zzzzqqq"}).json()["data"] == [], "a search that finds nothing is an empty list"
    bogus = api.get("/v1/matches", params={"bogus": 1})
    assert bogus.status_code == 400 and bogus.headers["cache-control"] == "no-store" and bogus.json()["parameter"] == "bogus"
    assert api.get("/v1/matches", params={"format": "XYZ"}).status_code == 422
    assert api.post("/v1/matches").status_code == 405 and api.get("/v1/nothing").status_code == 404
    first = api.get("/v1/matches", params={"limit": 1})
    etag = first.headers["etag"]
    assert api.get("/v1/matches", params={"limit": 1}, headers={"If-None-Match": etag}).status_code == 304
    cursor = first.json()["next_cursor"]
    assert api.get("/v1/matches", params={"limit": 1, "cursor": cursor + "x"}).status_code == 400, "a changed cursor"
    assert api.get("/v1/players/nobody/batting").status_code == 404 and api.get("/v1/teams/record", params={"team": "England", "gender": "male", "date_from": "2999-01-01"}).status_code == 200
    assert api.get("/v1/head-to-head", params={"team": "England", "opponent": "England", "gender": "male"}).status_code == 422


def test_date_filters_select_whole_matches_by_start_date_as_the_guide_says(api):
    """The 2019 Headingley Test started on 22 August and ran to the 25th. Whichever of its days the range ends on, it is in whole if it starts inside the range."""
    stokes = "e087956b"
    start = api.get(f"/v1/players/{stokes}/batting", params={"format": "TEST", "date_from": "2019-08-22", "date_to": "2019-08-22"}).json()["data"]
    assert (start["matches"], start["innings"], start["runs"]) == (1, 2, 143), "both innings, which were played on different days"
    later_day = api.get(f"/v1/players/{stokes}/batting", params={"format": "TEST", "date_from": "2019-08-23", "date_to": "2019-08-25"}).json()["data"]
    assert (later_day["matches"], later_day["runs"]) == (0, 0), "days 2 to 4 select nothing, because the match started before them"
    ends_before = api.get(f"/v1/players/{stokes}/batting", params={"format": "TEST", "date_from": "2019-08-15", "date_to": "2019-08-21"}).json()["data"]
    assert ends_before["matches"] == 0, "a range that ends the day before the match started does not include it (Stokes played no Test that started in that gap)"


def test_a_429_has_retry_after_as_the_guide_says(dataset):
    tight = TestClient(create_app(Settings(data_dir=dataset, cursor_secret="x" * 32, rate_per_minute=60, rate_burst=2)), base_url="http://testserver")
    try:
        codes = [tight.get("/v1/meta") for _ in range(3)]
        assert [c.status_code for c in codes] == [200, 200, 429] and int(codes[2].headers["retry-after"]) >= 1 and codes[2].headers["content-type"].startswith("application/problem+json")
        assert tight.get("/health").status_code == 200, "liveness is free"
        assert tight.get("/status").status_code == 429, "/status is counted, and only /health is not"
    finally:
        tight.app.state.db.close()


# Icons, the no-transform mark, and the developer details ---------------------------------------------------------------

def test_the_page_names_its_icons_and_each_is_served_with_the_pages_policy(client):
    """Without an icon link the browser asks for /favicon.ico, gets a 404 and may show an icon it remembers from another address (the owner's portfolio icon appeared on this page)."""
    page = client.get("/docs")
    found = [a for t, a in tags(page.text) if t == "link" and a.get("rel") in ("icon", "apple-touch-icon")]
    assert [(a["rel"], a["href"], a.get("type"), a.get("sizes")) for a in found] == [
        ("icon", "/docs/assets/brand-mark.svg", "image/svg+xml", None), ("icon", "/docs/assets/icon-32.png", "image/png", "32x32"),
        ("apple-touch-icon", "/docs/assets/apple-touch-icon.png", None, "180x180")]
    assets = Path(__file__).resolve().parents[1] / "cricket_api" / "docsite" / "assets"
    for a in found:
        r = client.get(a["href"])
        assert r.status_code == 200 and r.headers["content-security-policy"].startswith("default-src 'none'") and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "public, max-age=3600" and r.content == (assets / a["href"].rsplit("/", 1)[1]).read_bytes()
        again = client.get(a["href"], headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304
    svg, small, big = (client.get(a["href"]) for a in found)
    assert svg.headers["content-type"].startswith("image/svg+xml") and b"<script" not in svg.content
    assert small.content[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(small.content[16:20], "big") == 32 and int.from_bytes(small.content[20:24], "big") == 32
    assert big.content[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(big.content[16:20], "big") == 180
    assert client.get("/favicon.ico").status_code == 200, "the root icon is served too (tests/test_root_icons.py)"


def test_the_policy_still_allows_nothing_for_scripts_and_the_icons_need_no_new_source(client):
    page = client.get("/docs")
    policy = page.headers["content-security-policy"]
    assert "img-src 'self'" in policy and "font-src 'self'" in policy and "script" not in policy and "connect" not in policy
    assert re.findall(r"font-src ([^;]*)", policy) == ["'self'"], "fonts only from this host: the one precise addition for Outfit"


def test_only_the_page_is_marked_no_transform_so_cloudflare_adds_no_script_to_it(client):
    """Cloudflare's automatic Web Analytics injects <script src=static.cloudflareinsights.com/...> into HTML it may change. This page's policy blocks scripts, so every visit logged an error."""
    page = client.get("/docs")
    assert page.headers["cache-control"] == "public, max-age=3600, no-transform"
    assert client.get("/docs", headers={"If-None-Match": page.headers["etag"]}).headers["cache-control"] == "public, max-age=3600, no-transform", "a 304 says the same"
    for other in ("/docs/assets/docs.css", "/v1/meta", "/openapi.json"):
        assert "no-transform" not in client.get(other).headers["cache-control"]
    assert "cloudflareinsights" not in page.text, "the origin never carries the injected script; Cloudflare adds it on the way out"


def test_the_overview_and_a_contact_section_carry_the_developer_details(client):
    page = client.get("/docs")
    text = page.text
    overview = re.search(r'<section class="doc" id="overview">(.*?)</section>', text, re.S).group(1)
    assert "Developed and maintained by Ari Adaikalam." in overview
    assert '<a href="https://ariadaikalam.com">Portfolio</a>' in overview and '<a href="mailto:ariadaikalam1234@gmail.com">Contact</a>' in overview
    contact = re.search(r'<section class="doc" id="contact">(.*?)</section>', text, re.S).group(1)
    assert "<h2>Developer &amp; Contact</h2>" in text
    assert "Ari Adaikalam" in contact and '<a href="https://ariadaikalam.com">ariadaikalam.com</a>' in contact and '<a href="mailto:ariadaikalam1234@gmail.com">ariadaikalam1234@gmail.com</a>' in contact
    assert "Bug reports, data discrepancies and suggestions are welcome" in contact
    for asked in ("endpoint", "parameters", "request ID if available", "request_id", "X-Request-ID", "cached successful answer may carry none"):
        assert asked in contact
    assert "Never send passwords, tokens or keys" in contact
    assert text.index('id="reference"') < text.index('id="contact"'), "the contact section follows the reference"
    assert "Contact" in re.search(r"<nav.*?</nav>", text, re.S).group(0)


def test_the_cricsheet_attribution_is_separate_and_intact(client):
    page = client.get("/docs")
    footer = re.search(r"<footer>(.*?)</footer>", page.text, re.S).group(1)
    attribution = client.app.state.db.meta["attribution"]
    assert footer == f"<p>{inline(attribution)}</p>", "the footer is the data's own attribution, unchanged"
    assert "Cricsheet" in footer and "Open Data Commons Attribution License" in footer
    contact = re.search(r'<section class="doc" id="contact">(.*?)</section>', page.text, re.S).group(1)
    assert "Cricsheet" not in contact and "Ari" not in footer, "the data credit and the developer details are not mixed"
    assert 'rel="license"' not in page.text and client.get("/docs").headers["link"].startswith("<https://opendatacommons.org/licenses/by/1-0/>")


def test_the_only_addresses_the_page_points_to_are_the_published_ones(client):
    page = client.get("/docs")
    hrefs = {a["href"] for t, a in tags(page.text) if t == "a"}
    outside = {h for h in hrefs if not h.startswith("#")}
    assert outside <= {"https://cricsheet.org/", "https://opendatacommons.org/licenses/by/1-0/", "https://ariadaikalam.com", "mailto:ariadaikalam1234@gmail.com"}, outside
    assert "github" not in page.text.lower() and "<form" not in page.text and "<script" not in page.text


OVERVIEW_NOTICE = "Statistics reflect the matches available in this Cricsheet snapshot and may not represent complete career records or official totals."
COVERAGE_NOTICE = ("CricAtlas calculates statistics from the matches included in its Cricsheet snapshot. Cricsheet does not provide every cricket match, and some matches are withheld or unavailable. "
                   "Consequently, runs, wickets, appearances, centuries and other totals may differ from official career statistics. A missing match is absent data, not a zero-performance result. "
                   "Use <code>/v1/meta</code> to check the snapshot date and version.")


def test_the_overview_and_the_coverage_section_say_the_statistics_are_not_official_totals(client):
    text = client.get("/docs").text
    overview = re.search(r'<section class="doc" id="overview">(.*?)</section>', text, re.S).group(1)
    coverage = re.search(r'<section class="doc" id="coverage">(.*?)</section>', text, re.S).group(1)
    assert f"<p>{OVERVIEW_NOTICE}</p>" in overview
    assert f"<p>{COVERAGE_NOTICE}</p>" in coverage, "the coverage wording is exact, with /v1/meta as code"
    assert overview.index("Developed and maintained") < overview.index(OVERVIEW_NOTICE), "the notice follows the facts table"
    assert "Cricsheet" in text and "withheld" in coverage and "absent data" in coverage


def test_the_notices_change_nothing_in_the_responses_the_schema_or_the_data_version(client):
    meta = client.get("/v1/meta").json()
    assert set(meta) == {"data", "source"} and client.get("/status").json()["data_version"] == meta["data"]["data_version"]
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if "notice" in p], "no route was added for them"


def test_the_apps_font_is_served_from_this_host_with_the_pages_policy_and_is_the_documents_typeface(client):
    page = client.get("/docs")
    link = [a for t, a in tags(page.text) if t == "link" and a.get("rel") == "preload"]
    assert link == [{"rel": "preload", "href": "/docs/assets/outfit-latin.woff2", "as": "font", "type": "font/woff2", "crossorigin": None}] or [a["href"] for a in link] == ["/docs/assets/outfit-latin.woff2"]
    font = client.get("/docs/assets/outfit-latin.woff2")
    assets = Path(__file__).resolve().parents[1] / "cricket_api" / "docsite" / "assets"
    assert font.status_code == 200 and font.headers["content-type"].startswith("font/woff2") and font.content[:4] == b"wOF2" and font.content == (assets / "outfit-latin.woff2").read_bytes()
    assert font.headers["content-security-policy"].startswith("default-src 'none'") and font.headers["x-content-type-options"] == "nosniff" and font.headers["cache-control"] == "public, max-age=3600"
    assert client.get("/docs/assets/outfit-latin.woff2", headers={"If-None-Match": font.headers["etag"]}).status_code == 304
    css = client.get("/docs/assets/docs.css").text
    assert '--sans: "Outfit", ui-sans-serif, system-ui' in css and "font-display: swap" in css, "Outfit first, the system fonts behind it"
    assert len(font.content) < 40_000 and (assets / "OFL-Outfit.txt").read_text().startswith("Copyright 2021 The Outfit Project Authors"), "small, and its licence is kept beside it"


def test_the_stylesheet_keeps_the_apps_palette_white_focus_rings_and_reduced_motion():
    css = (Path(__file__).resolve().parents[1] / "cricket_api" / "docsite" / "assets" / "docs.css").read_text()
    for token in ("--bg: #141414", "--accent: #c4313a", "--link: #ebc862", "--muted: #a8a8a8", "--surface: #1d1d1d"):
        assert token in css, token
    assert ":focus-visible { outline: 2px solid #fff" in css, "the brand rule: focus rings are white"
    assert "prefers-reduced-motion: reduce" in css and "animation: none !important" in css and "color-scheme: dark" in css
    assert "backdrop-filter" in css and "@supports (backdrop-filter" in css, "the blur is an enhancement behind a support check"


def test_there_is_no_progress_bar_and_no_timeline_only_a_plain_contents_list(client):
    page = client.get("/docs").text
    css = client.get("/docs/assets/docs.css").text
    assert "progress" not in page.lower() and ".progress" not in css and "timeline" not in css.lower().replace("animation-timeline: view()", ""), "nothing fills or tracks the scroll position"
    assert "animation-timeline: scroll(" not in css and "nav::before" not in css and "nav::after" not in css and "nav a::before" not in css
    assert "nav a:target-current" in css, "the section being read is still marked, where the browser supports it"
    assert re.search(r"@media \(prefers-reduced-motion: reduce\)[^}]*\{[^}]*animation: none !important", css, re.S)


def test_the_footer_uses_the_full_width_so_the_data_credit_is_one_line_on_a_desktop(client):
    css = client.get("/docs/assets/docs.css").text
    footer = re.search(r"footer p \{([^}]*)\}", css).group(1)
    assert "max-width" not in footer, "a width cap on the footer text wrapped the credit onto two lines"


# Contents links must land on their section --------------------------------------------------------------------------------------------------------------------

def test_every_contents_link_has_exactly_one_target_and_it_is_the_section_its_label_names(page):
    nav = re.search(r'<nav aria-label="Contents">(.*?)</nav>', page.text, re.S).group(1)
    ids = re.findall(r'\sid="([^"]+)"', page.text)
    assert len(ids) == len(set(ids)), "a duplicate id would send a link to the first of them"
    links = re.findall(r'<a href="(#[^"]*)">([^<]*)</a>', nav)
    assert len(links) >= 15
    for target, label in links:
        assert ids.count(target[1:]) == 1, f"{label}: {target} has no single target"
        assert not re.search(r"<base\b", page.text, re.I)


def test_nothing_on_the_page_skips_drawing_so_a_link_is_not_aimed_at_a_guessed_height(client_module):
    """content-visibility makes the browser use a placeholder height for blocks it has not drawn. A link into the page was then aimed at the wrong place and the page shifted as the blocks above were drawn."""
    css = client_module.get("/docs/assets/docs.css").text
    assert "content-visibility: auto" not in css and "contain-intrinsic-size" not in css


def test_the_contents_is_a_drawer_opened_and_closed_by_links_alone_so_it_needs_no_script(page, client_module):
    """On a phone the contents is a panel that slides in from the right over a dimmed page, like the app's menu. The page runs no script, so it is open exactly while the address ends in #contents (:target).
    Every way out is a link: a section link, the dimmed page and the cross all change the address, and #closed is a fixed empty element so closing does not scroll."""
    text = page.text
    assert '<a class="burger" href="#contents"' in text and 'aria-label="Open the contents"' in text
    assert '<span id="contents" hidden></span><div class="menu" id="contents-panel">' in text and '<details class="menu"' not in text, "a hidden marker is the target, just before the panel"
    assert 'aria-controls="contents-panel"' in text
    assert '<a class="scrim" href="#closed"' in text and '<a class="close" href="#closed"' in text and '<span id="closed"></span>' in text and '<span class="drawer-title">Contents</span>' in text
    assert not re.search(r"<script|\sonclick=", text), "no script"
    css = client_module.get("/docs/assets/docs.css").text
    phone = css[css.index("@media (max-width: 899px)"):css.index("/* Motion:")]
    for needle in ("#contents:target + .menu nav { transform: none; }", "#contents:target + .menu .scrim { opacity: 1; }", "#contents:target + .menu { visibility: visible;"):
        assert needle in phone, needle
    menu = re.search(r"\n  \.menu \{([^}]*)\}", phone).group(1)
    assert "position: fixed" in menu and "visibility: hidden" in menu, "hidden from the keyboard and screen readers while closed"
    assert "#closed { display: block; position: fixed" in phone, "closing does not move the page"
    assert re.search(r"\.burger, \.scrim, \.close, \.drawer-title, #closed, #contents \{ display: none; \}", css), "none of it shows beside the always-open sidebar on a wide screen, and the marker has no box"
    assert ".menu:target" not in css, "the panel itself is never the target: WebKit scrolls the page to a fixed target"
    assert "::details-content" not in phone
    assert "<svg" not in text.split("<main")[0].split('<div class="shell"')[1], "the cross is drawn with CSS like the three lines, so the header holds one inline mark"


def test_the_sidebar_on_a_wide_screen_is_still_always_there_and_sticky(client_module):
    css = client_module.get("/docs/assets/docs.css").text
    assert re.search(r"\n\.menu \{ position: sticky; top: var\(--top\);", css)
