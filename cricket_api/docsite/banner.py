"""The banner at the top of the repository's README, drawn from the live data.

It is an SVG built once when the API starts, from the same ranking the leaderboard route uses, so it shows what that request returns now and never has to be redrawn by hand. It is served as a documentation
asset (see routes/docs.py). GitHub shows an image through a proxy that blocks fonts inside the file, so the Outfit text is drawn as outlines (banner-text.json, made once from the font)
and the ball is inside the file. Only the code panel is live text, in the reader's monospace font. Nothing refers to another address."""
import json
import re
from xml.sax.saxutils import escape

from cricket_api import leaderboards, metrics

ASSET = "banner.svg"
ROUTE = "/v1/leaderboards/batting"
# The request the banner shows. Its three leaders are the rows drawn, so the picture and the route cannot differ.
QUERY = (("metric", "runs"), ("collection", "ipl"), ("limit", "3"))
LIMIT = 3

PAGE, PANEL, RED, SKY, CREAM, MUTED, WHITE = "#141414", "#1B1B1B", "#C4313A", "#56B4E9", "#E6D2A3", "#A8A8A8", "#FFFFFF"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def leaders(db) -> list[dict]:
    """The three rows the request above returns, as the leaderboard route builds them."""
    params = dict(QUERY)
    matches_sql, match_params = metrics.selected_matches(params["collection"], None, None, None, None, None)
    balls, other = leaderboards.defaults("batting", params["metric"])
    rows = leaderboards.rank(db, "batting", matches_sql, match_params, params["metric"], LIMIT, balls, other)
    return [{"rank": r["rank"], "name": r["name"], "value": r["shown"]} for r in rows]


def _span(text: str, colour: str, bold: bool = False) -> str:
    weight = ' font-weight="700"' if bold else ""
    return f'<tspan fill="{colour}"{weight}>{escape(str(text))}</tspan>'


def _line(x: int, y: int, klass: str, *spans: str) -> str:
    return f'<text class="{klass}" x="{x}" y="{y}" xml:space="preserve">{"".join(spans)}</text>'


def _mark(assets) -> str:
    """The CricAtlas ball, placed in the logo. It is the same file the documentation page uses."""
    svg = re.sub(r">\s+<", "><", (assets / "brand-mark.svg").read_text().strip())
    svg = svg.replace(' xmlns="http://www.w3.org/2000/svg"', "").replace(' width="512" height="512"', "")
    return svg.replace("<svg ", '<svg x="80" y="62" width="58" height="58" ', 1)


def _request_line(y: int) -> str:
    query = "".join(
        _span("?" if i == 0 else "&", MUTED) + _span(key, SKY) + _span("=", MUTED) + _span(value, CREAM) for i, (key, value) in enumerate(QUERY))
    return _line(623, y, "q", _span("GET", RED, True), _span(" " + ROUTE, WHITE), query)


def _response(rows: list[dict], top: int) -> list[str]:
    step, out = 32, []
    lines = [(0, [_span("{ ", WHITE), _span('"leaders"', SKY), _span(": [", WHITE)])]
    for i, row in enumerate(rows):
        end = "," if i < len(rows) - 1 else ""
        quoted = '"%s"' % row["name"]
        lines.append((2, [_span("{ ", WHITE), _span('"rank"', SKY), _span(": %s, " % row["rank"], WHITE), _span('"name"', SKY), _span(": ", WHITE), _span(quoted, CREAM), _span(",", WHITE)]))
        lines.append((5, [_span('"value"', SKY), _span(": ", WHITE), _span(row["value"], WHITE, True), _span(" }" + end, WHITE)]))
    lines.append((0, [_span("] }", WHITE)]))
    for n, (indent, spans) in enumerate(lines):
        out.append(_line(623 + indent * 12, top + n * step, "m", *spans))
    return out


def build(db, assets) -> bytes:
    """The banner for the data in `db`. `assets` is the folder with the font and the ball."""
    rows = leaders(db)
    summary = ", ".join("%s %s" % (r["name"], r["value"]) for r in rows)
    text = json.loads((assets / "banner-text.json").read_text())
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" width="1280" height="640" role="img" aria-labelledby="t d">',
        '<title id="t">CricAtlas API</title>',
        f'<desc id="d">A request for the three leading IPL run scorers and the answer: {escape(summary)}.</desc>',
        f'<defs><style>.m{{font-family:{MONO};font-size:20px}}.q{{font-family:{MONO};font-size:15px}}</style>'
        '<radialGradient id="g1" cx="84%" cy="50%" r="55%"><stop offset="0" stop-color="#C4313A" stop-opacity=".30"/><stop offset="1" stop-color="#C4313A" stop-opacity="0"/></radialGradient>'
        '<radialGradient id="g2" cx="0%" cy="100%" r="50%"><stop offset="0" stop-color="#C4313A" stop-opacity=".16"/><stop offset="1" stop-color="#C4313A" stop-opacity="0"/></radialGradient></defs>',
        f'<rect width="1280" height="640" fill="{PAGE}"/><rect width="1280" height="640" fill="url(#g1)"/><rect width="1280" height="640" fill="url(#g2)"/>',
        _mark(assets),
        f'<path fill="{WHITE}" d="{text["logo"]["white"]}"/><path fill="{RED}" d="{text["logo"]["red"]}"/>',
        f'<path fill="{MUTED}" d="{text["tag"]}"/>',
        f'<path fill="{WHITE}" d="{text["title"]}"/>',
        f'<path fill="#D0D0D0" d="{text["tagline"]}"/>',
        f'<rect x="595.5" y="130.5" width="624" height="369" rx="14" fill="{PANEL}" stroke="#FFFFFF" stroke-opacity=".12"/>',
        _request_line(188),
        *_response(rows, 233),
        f'<path fill="{MUTED}" d="{text["note"]}"/>',
        f'<rect y="630" width="1280" height="10" fill="{RED}"/>',
        "</svg>",
    ]
    return "".join(body).encode("utf-8")
