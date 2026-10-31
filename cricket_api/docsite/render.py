"""The documentation page, built from the running server: the guide, the examples, and a reference made from the OpenAPI document so that it cannot disagree with the routes."""
import html
import json
import re
from pathlib import Path

from cricket_api import admission, caching, leaderboards, search
from cricket_api.docsite import guide
from cricket_api.docsite.examples import EXAMPLES, Kind, fill, lookup
from cricket_api.docsite.guide import code, inline, p, shown, table

COMMON_MIN_ROUTES = 8   # a query parameter that is written the same way on at least this many routes is documented once, in "Common parameters"
GROUPS = (
    ("Server", ("/health", "/status", "/freshness", "/v1/meta"), "Server", "Check the server, and read the data version, row counts and licence."),
    ("Lookups", ("/v1/competitions", "/v1/teams", "/v1/venues", "/v1/players"), "Lookups", "Find ids and names: competitions, teams, venues and players."),
    ("Matches", ("/v1/matches",), "Matches", "List matches, then read one match, its scorecard, its balls and its notes."),
    ("Players", ("/v1/players/",), "Players", "A player's career, season by season and by split, and the matches they played."),
    ("Leaderboards", ("/v1/leaderboards/",), "Leaderboards", "The top players for a batting or bowling statistic."),
    ("Matchups", ("/v1/matchups",), "Matchups", "One batter against one bowler."),
    ("Teams and head-to-head", ("/v1/teams/record", "/v1/head-to-head"), "Teams", "Team results, and one team against another."),
)
ORDER = {"/v1/teams/record": 6, "/v1/head-to-head": 6}


def group_of(path: str) -> str:
    """The reference group of a route. The most specific prefix wins, so /v1/teams/record is Teams and head-to-head and /v1/teams is a lookup."""
    best, name = -1, "Other"
    for title, prefixes, _, _ in GROUPS:
        for prefix in prefixes:
            exact = path == prefix or (prefix.endswith("/") and path.startswith(prefix)) or (not prefix.endswith("/") and path.startswith(prefix + "/"))
            if exact and len(prefix) > best:
                best, name = len(prefix), title
    return name


def group_id(title: str) -> str:
    return "group-" + title.lower().replace(" ", "-")


def slug(path: str) -> str:
    return "route-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def ref(schema: dict, components: dict) -> str:
    """The type of a schema, written for a reader: string, integer, a date, a list of X, one of the allowed values, or null."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        parts = [ref(s, components) for s in schema["anyOf"]]
        non_null = [x for x in parts if x != "null"]
        return " or ".join([*non_null, "null"]) if len(non_null) != len(parts) else " or ".join(parts)
    if "enum" in schema:
        return "one of " + ", ".join(str(v) for v in schema["enum"])
    kind = schema.get("type", "value")
    if kind == "array":
        return "list of " + ref(schema.get("items", {}), components)
    if kind == "object" and "additionalProperties" in schema:
        return "map of " + ref(schema["additionalProperties"], components)
    if schema.get("format") == "date":
        return "date"
    if kind == "string" and "pattern" in schema:
        return "string (an id)"
    return {"integer": "integer", "number": "number", "boolean": "true or false", "string": "string", "null": "null"}.get(kind, kind)


def bounds(schema: dict) -> str:
    schema = next((s for s in schema.get("anyOf", []) if s.get("type") != "null"), schema)
    parts = []
    if "minimum" in schema:
        parts.append(f"at least {schema['minimum']}")
    if "maximum" in schema:
        parts.append(f"at most {schema['maximum']}")
    if "minLength" in schema:
        parts.append(f"{schema['minLength']} or more characters")
    if "maxLength" in schema:
        parts.append(f"at most {schema['maxLength']} characters")
    return ", ".join(parts)


def model_rows(name: str, components: dict) -> list[list[str]]:
    schema = components["schemas"].get(name, {})
    required = set(schema.get("required", []))
    return [[f"`{field}`", ref(spec, components), spec.get("description", "").split(". ")[0] or ""] for field, spec in schema.get("properties", {}).items() if field not in ("source",)] or [[f"`{r}`", "", ""] for r in required]


def data_model(operation: dict, components: dict) -> tuple[str, str | None]:
    """(name of the response model, name of the model inside its `data` field) for a 200 response."""
    top = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    data = components["schemas"][top]["properties"].get("data", {})
    inner = data.get("$ref") or data.get("items", {}).get("$ref")
    return top, (inner.rsplit("/", 1)[-1] if inner else None)


def cache_note(path: str) -> str:
    policy = caching.policy_for(path)
    return "not stored" if policy.max_age is None else f"{caching.cache_control(policy)} ({policy.name})"


def cache_label(path: str) -> str:
    """The short form for a route's card: the policy's name, which the Caching table turns into a Cache-Control value."""
    policy = caching.policy_for(path)
    return "not stored" if policy.max_age is None else policy.name


def admission_note(path: str) -> str:
    kind = admission.TABLE.get(path)
    return "not admitted, no database work" if kind in (None, admission.NONE) else kind


def field_list(fields: list[list[str]]) -> str:
    """The fields of a response model as one aligned block, a name and a type on each line. The models carry no per-field descriptions, so a table with an empty column would only add markup."""
    names = [f[0].strip("`") for f in fields]
    width = max(len(n) for n in names)
    lines = [f"{n.ljust(width)}  {f[1]}" + (f"  {f[2]}" if f[2] else "") for n, f in zip(names, fields)]
    return f'<pre class="fields"><code>{html.escape(chr(10).join(lines))}</code></pre>'


def parameter_key(prm: dict) -> tuple:
    return (prm["name"], prm["in"], json.dumps(prm["schema"], sort_keys=True), prm.get("description", ""), bool(prm.get("required")))


def common_parameters(openapi: dict) -> list[dict]:
    """The query parameters that are written exactly the same way on many routes (the filters, mostly), in the order they first appear. They are documented once and each route names the ones it takes."""
    seen: dict = {}
    for item in openapi["paths"].values():
        for prm in item["get"].get("parameters", []):
            key = parameter_key(prm)
            seen.setdefault(key, [prm, 0])[1] += 1
    return [prm for prm, n in seen.values() if n >= COMMON_MIN_ROUTES and prm["in"] == "query"]


def parameter_row(prm: dict, components: dict) -> list[str]:
    schema = prm["schema"]
    tags = (" <span class=\"tag\">path</span>" if prm["in"] == "path" else "") + (" <span class=\"tag req\">required</span>" if prm.get("required") else "")
    notes = "; ".join(x for x in (bounds(schema), f"default {json.dumps(schema['default'])}" if "default" in schema else "", prm.get("description", "").split(". ")[0]) if x)
    return [f"<code>{html.escape(prm['name'])}</code>{tags}", ref(schema, components), notes]   # the first cell is markup made here, the others are text


def parameter_table(rows: list[list[str]]) -> str:
    body = "".join(f"<tr><td>{r[0]}</td><td>{inline(r[1])}</td><td>{inline(r[2])}</td></tr>" for r in rows)
    return f'<div class="table params"><table><thead><tr><th>Parameter</th><th>Type</th><th>Notes</th></tr></thead><tbody>{body}</tbody></table></div>'


def reference(openapi: dict, costs: dict) -> str:
    components, parts = openapi["components"], []
    common = common_parameters(openapi)
    common_keys = {parameter_key(prm) for prm in common}
    grouped: dict[str, list] = {}
    for path, item in openapi["paths"].items():
        grouped.setdefault(group_of(path), []).append((path, item["get"]))
    parts.append('<section class="common" id="common-parameters"><h3>Common parameters</h3>'
                 + p("These query parameters are written the same way on many routes. A route lists the ones it takes under its summary.")
                 + parameter_table([parameter_row(prm, components) for prm in common]) + "</section>")
    for title, _, _, blurb in GROUPS:
        routes = sorted(grouped.get(title, []), key=lambda r: r[0])
        if not routes:
            continue
        parts.append(f'<section class="group" id="{group_id(title)}"><h3>{html.escape(title)}</h3>{p(blurb)}')
        for path, op in routes:
            own = [prm for prm in op.get("parameters", []) if parameter_key(prm) not in common_keys]
            shared = [prm["name"] for prm in op.get("parameters", []) if parameter_key(prm) in common_keys]
            top, inner = data_model(op, components)
            fields = model_rows(inner, components) if inner else model_rows(top, components)
            errors = ", ".join(sorted(c for c in op["responses"] if c[0] in "45"))
            cost = costs[path]
            chips = [f"Cost {cost} token{'s' if cost != 1 else ''}" if cost else "Not counted against the rate limit", f"Cache: {cache_label(path)}", f"Errors: {errors}"]   # the policy names are decoded in Caching, and the load class of every route is listed in Limits
            block = [f'<section class="route" id="{slug(path)}"><h4><span class="method">GET</span> <code>{html.escape(path)}</code></h4>', p(op.get("summary", "")),
                     '<p class="meta">' + "".join(f'<span class="chip">{html.escape(c)}</span>' for c in chips) + "</p>"]
            if shared:
                block.append('<p class="also">Also takes ' + " ".join(f"<code>{html.escape(n)}</code>" for n in shared) + ' <a href="#common-parameters">(common parameters)</a></p>')
            if own:
                block.append(parameter_table([parameter_row(prm, components) for prm in own]))
            elif not shared:
                block.append('<p class="none">No parameters.</p>')
            if fields:
                block.append(f"<details><summary>Response fields <code>{html.escape(inner or top)}</code></summary>{field_list(fields)}</details>")
            block.append("</section>")
            parts.append("".join(block))
        parts.append("</section>")
    return "".join(parts)


def url(path: str, params: dict) -> str:
    from urllib.parse import quote
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return path + ("?" + query if query else "")


def example_html(example, level: int = 3, show_values: bool = False) -> str:
    kept: dict = {}
    steps = []
    for number, step in enumerate(example.steps, 1):
        path = fill(step.path, kept)
        params = {k: ("NEXT_CURSOR" if v == "{cursor}" else fill(v, kept)) for k, v in step.params.items()}
        for name, source in step.take.items():
            value = step.shows.get(source)
            kept[name] = value.description if isinstance(value, Kind) else value
        width = max(len(field) for field in step.shows)
        values = "\n".join(f"{field.ljust(width)}  {shown(value)}" for field, value in step.shows.items())
        note = " ".join(x for x in (["Needs the full dataset."] if step.full_data else []) + ([f"Keep `{n}` for the next step." for n in step.take][:1] if step.take else []))
        steps.append(f'<div class="step"><h{level + 1}><span class="num">{number}</span> {html.escape(step.title)}</h{level + 1}>{p(step.explain)}'
                     f"{code('GET ' + url(path, params))}"
                     f'<details{" open" if show_values else ""}><summary>Values in the response</summary><pre class="values"><code>{html.escape(values)}</code></pre></details>{p(note) if note else ""}</div>')
    return f'<div class="example" id="example-{example.id}"><h{level}>{html.escape(example.title)}</h{level}>{p(example.intro)}{"".join(steps)}</div>'


def quickstart_html(server: str | None = None) -> str:
    """The first example in full, as the quickstart, and the others each behind one line. `server` is the address to show when the page is served under a prefix (the web variant)."""
    first, *rest = EXAMPLES
    more = "".join(f'<details class="more" id="more-{e.id}"><summary>{html.escape(e.title)}</summary>{example_html(e, 4)}</details>' for e in rest)
    lead = (f"The address of this server is `{server}`. Add it in front of the path, for example `curl \"{server}/v1/players?q=Stokes\"`." if server
            else "Add the address of this server in front of the path, for example `curl \"$API/v1/players?q=Stokes\"`.")
    return (p(lead + " The values shown are checked examples from a fixed test snapshot, not live statistics. New snapshots may return different values.")
            + example_html(first, show_values=True) + '<h3 id="more-examples">More examples</h3>' + more)


def collect(app, db, settings, cost_for) -> dict:
    """The facts the guide quotes, read from the running server."""
    meta = db.meta
    rows = {name: t["rows"] for name, t in meta["tables"].items()}
    runs = db.query("SELECT count(*) AS n, count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM wicket_fielders f WHERE f.match_id = w.match_id AND f.innings = w.innings "
                    "AND f.delivery_number = w.delivery_number AND f.wicket_number = w.wicket_number)) AS missing FROM wickets w WHERE w.wicket_type = 'run out'")[0]
    openapi = app.openapi()
    cursor_routes, capped_routes, costs = [], [], {}
    for path, item in openapi["paths"].items():
        op = item["get"]
        costs[path] = cost_for(re.sub(r"\{[^}]+\}", "x", path))
        names = {prm["name"] for prm in op.get("parameters", [])}
        for prm in op.get("parameters", []):
            if prm["name"] == "limit":
                schema = prm["schema"]
                (cursor_routes if "cursor" in names else capped_routes).append((path, schema.get("default"), schema.get("maximum")))
    by_cost: dict[int, list] = {}
    for path, cost in sorted(costs.items()):
        by_cost.setdefault(cost, []).append(path)
    lb = leaderboards
    return {**app.state.dataset_summary, "public_url": settings.public_url, "data_version": meta["data_version"], "snapshot_date": meta.get("snapshot_date"), "built": meta["built_at_utc"][:10], "matches": rows["matches"], "balls": rows["balls"], "players": rows["players"],
            "run_out_gap": round(100 * runs["missing"] / runs["n"]) if runs["n"] else 0, "cursor_routes": sorted(cursor_routes), "capped_routes": sorted(capped_routes),
            "ipv6_prefix": settings.ipv6_prefix, "limits": app.state.limits, "rate_per_minute": settings.rate_per_minute, "rate_burst": settings.rate_burst, "costs": sorted(by_cost.items()), "costs_by_path": costs,
            "licence": meta["licence"], "licence_url": meta["licence_url"], "attribution": meta["attribution"],
            "search": {"query": search.MAX_QUERY, "words": search.MAX_KEYWORDS, "word": search.MAX_KEYWORD},
            "defaults": {"batting average": (lb.BATTING["average"].min_balls, lb.BATTING["average"].min_other), "batting strike rate": (lb.BATTING["strike_rate"].min_balls,),
                         "bowling average": (lb.BOWLING["average"].min_balls, lb.BOWLING["average"].min_other), "bowling economy": (lb.BOWLING["economy"].min_balls,)},
            "openapi": openapi}


def ver_html(facts: dict) -> str:
    """The data set in the top bar, named by the shared rule (dataset_display_name: a milestone label, else Data of its snapshot date, else the hash). A daily update is not a milestone, so it
    never gets a counting name. The overview always gives the technical id as well."""
    name = facts["dataset_display_name"]
    if name != facts["data_version"]:
        return html.escape(name)
    return f'data <code>{html.escape(facts["data_version"])}</code>'


def nav_html(sections: list) -> str:
    groups: dict[str, list] = {}
    for section in sections:
        groups.setdefault(section.group, []).append((section.id, section.nav))
    about = groups.pop("About", None)
    groups["Reference"] = [(group_id(title), nav) for title, _, nav, _ in GROUPS]
    if about:
        groups["About"] = about
    return "".join(f'<div class="navgroup"><p class="navhead">{html.escape(name)}</p><ul>' + "".join(f'<li><a href="#{i}">{html.escape(label)}</a></li>' for i, label in items) + "</ul></div>"
                   for name, items in groups.items())


TITLE = "CricAtlas API Documentation"
ASSETS = Path(__file__).resolve().parent / "assets"


def brand_mark() -> str:
    """The CricAtlas leather-ball mark (assets/brand-mark.svg), written into the page as it is. It is inline, so it costs no request, and the policy needs no image source for it. It is decorative: the
    brand name beside it says the same thing."""
    svg = re.sub(r">\s+<", "><", (ASSETS / "brand-mark.svg").read_text().strip())
    svg = svg.replace(' xmlns="http://www.w3.org/2000/svg"', "").replace(' width="512" height="512"', "")
    return svg.replace("<svg ", '<svg class="mark" aria-hidden="true" focusable="false" ', 1)


# The contents menu on a phone is a panel that slides in from the right over a dimmed page, as the app's menu is. The page has no script, so it is open exactly while the address ends in #contents (CSS
# :target). Following a link to a section, tapping the dimmed page or the cross changes the address, and that closes it. The address names a hidden, empty marker (#contents) placed just before the
# panel, and the panel opens by a sibling selector. A target that has no box is never scrolled to, so opening does not move the page: WebKit scrolled to a fixed panel used as the target, and moved
# the page up each time it opened. #closed is a fixed, empty element, so closing does not move it either.
BURGER = '<a class="burger" href="#contents" aria-label="Open the contents" aria-controls="contents-panel"><span></span><span></span><span></span></a>'
CLOSE = '<span class="drawer-title">Contents</span><a class="close" href="#closed" aria-label="Close the contents"><span></span><span></span></a>'
DESCRIPTION = "Ball-by-ball cricket data over a read-only JSON API: a quickstart, the rules of the data and a reference for every route."
SHARE_IMAGE = "/docs/assets/og-api-docs.png"


def share_tags(origin: str, base: str = "") -> str:
    """The Open Graph and Twitter tags for a link preview, with an absolute image URL. Nothing when the API's public origin is not configured, because a relative image address is not accepted by preview
    sites and the repository does not hold an installation's name."""
    if not origin:
        return ""
    image, e = origin + base + SHARE_IMAGE, html.escape
    return (f'<meta property="og:type" content="website"><meta property="og:title" content="{e(TITLE)}"><meta property="og:description" content="{e(DESCRIPTION)}">'
            f'<meta property="og:url" content="{e(origin + base)}/docs"><meta property="og:image" content="{e(image)}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta property="og:image:alt" content="{e(TITLE)}"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{e(TITLE)}">'
            f'<meta name="twitter:description" content="{e(DESCRIPTION)}"><meta name="twitter:image" content="{e(image)}">')


def page(facts: dict, base: str = "") -> str:
    """The documentation page. `base` is the path prefix it is served under ("" on the API's own host, "/api" in the web variant), so its asset links and share URLs are right where it is read."""
    openapi = facts["openapi"]
    assets = f"{base}/docs/assets"
    openapi_path = facts.get("openapi_path", "/openapi.json")
    sections = guide.sections(facts)
    by_id = {s.id: s for s in sections}
    body = [f'<section class="doc" id="overview"><h2>{html.escape(by_id["overview"].title)}</h2>{by_id["overview"].body}</section>',
            f'<section class="doc" id="quickstart"><h2>Quickstart</h2>{quickstart_html(facts.get("server"))}</section>']
    body += [f'<section class="doc" id="{s.id}"><h2>{html.escape(s.title)}</h2>{s.body}</section>' for s in sections[1:] if s.id != "contact"]
    body.append(f'<section class="doc" id="reference"><h2>Reference</h2>{p(f"Made from the OpenAPI document at `{openapi_path}`, so it matches the routes. Every route takes `GET` only.")}{reference(openapi, facts["costs_by_path"])}</section>')
    body += [f'<section class="doc" id="{s.id}"><h2>{html.escape(s.title)}</h2>{s.body}</section>' for s in sections if s.id == "contact"]
    nav_sections = [by_id["overview"], guide.Section("quickstart", "Quickstart", "Start", "Quickstart", "")] + sections[1:]
    badges = "".join(f'<li>{html.escape(b)}</li>' for b in ("Read-only", "JSON", "No API key", "Cacheable"))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{TITLE}</title><meta name=\"description\" content=\"Ball-by-ball cricket data over a read-only JSON API: a quickstart, the rules of the data and a reference for every route.\">"
            f"<link rel=\"icon\" type=\"image/svg+xml\" href=\"{assets}/brand-mark.svg\"><link rel=\"icon\" type=\"image/png\" sizes=\"32x32\" href=\"{assets}/icon-32.png\">"
            f"<link rel=\"apple-touch-icon\" sizes=\"180x180\" href=\"{assets}/apple-touch-icon.png\">"
            f"<link rel=\"preload\" href=\"{assets}/outfit-latin.woff2\" as=\"font\" type=\"font/woff2\" crossorigin>"
            f"{share_tags(facts.get('public_url', ''), base)}"
            f"<link rel=\"stylesheet\" href=\"{assets}/docs.css\"></head><body>"
            '<a class="skip" href="#content">Skip to the content</a>'
            f'<header class="top"><a class="brand" href="#top" aria-label="CricAtlas">{brand_mark()}<span>Cric<b>Atlas</b></span></a><span class="tag-line">API Documentation</span>'
            f'<span class="ver">{ver_html(facts)}</span>{BURGER}</header><span id="closed"></span>'
            f'<div class="shell" id="top"><span id="contents" hidden></span><div class="menu" id="contents-panel"><a class="scrim" href="#closed" tabindex="-1" aria-hidden="true"></a>'
            f'<nav aria-label="Contents">{CLOSE}{nav_html(nav_sections)}</nav></div>'
            f'<main id="content"><div class="hero"><h1>{TITLE}</h1><p class="lead">Ball-by-ball cricket data from Cricsheet, over a read-only JSON API.</p><div class="hb"><a class="btn p" href="#quickstart">Quickstart</a><a class="btn s" href="#reference">Reference</a></div><ul class="badges">{badges}</ul></div>{"".join(body)}</main></div>'
            f'<footer><p>{inline(facts["attribution"])}</p></footer></body></html>')
