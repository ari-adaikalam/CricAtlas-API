"""The written part of the documentation, kept short. Facts that can change (the data version, limits, costs, how many run outs lack a fielder) are filled in from the running server, never typed here.

How it is written: a reader arrives to get something done, so the page starts with a request that works, then the few ideas that every route shares, then the rules of the data, then the reference. Each guide answers one
question in a few lines and leaves exceptions to the reference. Headings are short noun phrases in sentence case, and the sidebar uses the same words.
"""
import html
import re
from dataclasses import dataclass

from cricket_api import admission, caching
from cricket_api.docsite.examples import Kind

SAFE_LINKS = ("https://cricsheet.org/", "https://opendatacommons.org/licenses/by/1-0/", "https://ariadaikalam.com", "mailto:ariadaikalam1234@gmail.com")


@dataclass(frozen=True)
class Section:
    id: str
    nav: str       # the label in the sidebar
    group: str     # the heading it sits under in the sidebar
    title: str     # the heading of the section
    body: str


ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def keep_dates_whole(markup: str) -> str:
    """Wrap each date in prose so it cannot wrap at its hyphens. Only text between tags is touched, never a tag or an address."""
    return "".join(part if part.startswith("<") else ISO_DATE.sub(r'<span class="nb">\g<0></span>', part) for part in re.split(r"(<[^>]*>)", markup))


def inline(text: str) -> str:
    """Escape the text, then turn `code`, **bold** and [text](link) into markup. A link is only made for the addresses in SAFE_LINKS."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)

    def link(match):
        return f'<a href="{match.group(2)}">{match.group(1)}</a>' if match.group(2) in SAFE_LINKS else match.group(0)

    return keep_dates_whole(re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, out))


def p(text):
    return f"<p>{inline(text)}</p>"


def ul(items):
    return "<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>"


def dl(items, css="facts"):
    return f'<dl class="{css}">' + "".join(f"<dt>{inline(t)}</dt><dd>{inline(d)}</dd>" for t, d in items) + "</dl>"


def table(header, rows):
    head = "".join(f"<th>{inline(h)}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{inline(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def code(text):
    return f"<pre><code>{html.escape(text)}</code></pre>"


def shown(value) -> str:
    import json
    return value.description if isinstance(value, Kind) else json.dumps(value)


def sections(f: dict) -> list[Section]:
    """The guide, in reading order. f is the facts the renderer collected from the running server."""
    d = f["defaults"]
    lim = f["limits"]
    classes = (admission.HEAVY, admission.MEDIUM, admission.LIGHT)
    return [
        Section("overview", "Overview", "Start", "Overview", "".join([
            p("A read-only HTTP API for ball-by-ball cricket data. Every request is a `GET`, every answer is JSON, and there are no keys or accounts."),
            dl([
                ("Data", f"[Cricsheet](https://cricsheet.org/)'s ball-by-ball records from {f['first_match_date'] or 'an unknown first date'} to {f['latest_match_date'] or 'an unknown latest date'}. "
                         f"The served data includes {len(f['collections'])} collections and these formats: {', '.join(f['formats']) or 'none'}. See `/v1/competitions` for the collections and events."),
                ("This copy", (f"Data version `{f['data_version']}`, built {f['built']}. " if f["dataset_display_name"] == f["data_version"] else f"**{f['dataset_display_name']}**, snapshot `{f['data_version']}`, built {f['built']}. ")
                           + f"It holds {f['matches']:,} matches, {f['balls']:,} balls and {f['players']:,} players. The snapshot id is the technical name: it is what `/status` and `/v1/meta` report and what keys caches and cursors."),
                ("Routes", "Business routes are under `/v1`. `/health`, `/status` and `/freshness` check the server and data. `/openapi.json` describes every route."),
                ("Licence", f"[{f['licence']}]({f['licence_url']}). Credit Cricsheet when you use the data. Every response has a `Link` header to the licence and an `X-Attribution` header, and data routes add a `source` block."),
                ("Developer", "Developed and maintained by Ari Adaikalam. [Portfolio](https://ariadaikalam.com) · [Contact](mailto:ariadaikalam1234@gmail.com)"),
            ]),
            '<div id="refresh-status"><!--cricatlas-refresh-status--></div>',
            p("Statistics reflect the matches available in this Cricsheet snapshot and may not represent complete career records or official totals."),
        ])),
        Section("coverage", "Coverage", "Guides", "Coverage", "".join([
            p(f"Everything here is about the matches in this dataset, dated {f['first_match_date'] or 'unknown'} to {f['latest_match_date'] or 'unknown'}. "
              f"Its formats are {', '.join(f['formats']) or 'none'} and its seasons are listed in `/v1/meta` under `coverage.seasons`. "
              "A player's career, a team's record and a leaderboard are all made from these matches, and none of them is a complete history."),
            p("CricAtlas calculates statistics from the matches included in its Cricsheet snapshot. Cricsheet does not provide every cricket match, and some matches are withheld or unavailable. "
              "Consequently, runs, wickets, appearances, centuries and other totals may differ from official career statistics. A missing match is absent data, not a zero-performance result. "
              "Use `/v1/meta` to check the snapshot date and version."),
            p("`/freshness` reports the latest match date, last successful source check and last successful data refresh where recorded. A check does not mean new data was activated. "
              "Unchanged data keeps its version and activation time. These times are separate from the snapshot's build time. A failed preparation leaves the previous verified snapshot live. "
              "A cache or public verification failure is recorded as pending, not as a completed refresh. Source availability depends on Cricsheet; a refresh is not live scoring."),
            p("Team record and head-to-head responses say so: `coverage.scope` is `this dataset`. `coverage` also gives the number of matches and the dates of the first and last of them, "
              "for the whole filtered result and not just the page returned. The dates are `null` when the result is empty."),
        ])),
        Section("ids", "IDs and search", "Guides", "IDs and search", "".join([
            p("Search to find an id, then use the id. Names have several spellings. Ids do not change."),
            table(["What", "Identified by", "Find it with"], [
                ["Match", "`match_id`, Cricsheet's match number", "`/v1/matches?q=`"],
                ["Player", "`player_id`, Cricsheet's id for the person", "`/v1/players?q=`, any spelling of the name"],
                ["Team", "its name and its `gender`, because men's and women's teams share names", "`/v1/teams?q=`"],
                ["Competition", "the Cricsheet code, such as `ipl`, `tests` or `hnd`", "`/v1/competitions?q=`"],
                ["Venue", "its name as Cricsheet records it", "`/v1/venues?q=`"],
            ]),
            ul(["`q` is a few words separated by spaces. Each word is matched, ignoring case, as a piece of a name, and every word has to match something. "
                "`England Australia Headingley 2019` finds that Test.",
                "Matches search the teams, event, competition code, venue, city and season. Competitions search the event name and code.",
                f"At most {f['search']['query']} characters, {f['search']['words']} different words and {f['search']['word']} characters in a word. A search that finds nothing is an empty list, not an error.",
                "`team` with `opponent` on `/v1/matches` selects a pairing in either order.",
                "A team that changed its name is more than one team. A season is Cricsheet's label, such as `2019` or `2019/20`."]),
        ])),
        Section("filters", "Filters", "Guides", "Filters", "".join([
            p("Statistics routes share these filters: `collection`, `format` (`TEST`, `ODI` or `T20`), `gender`, `season`, `date_from` and `date_to`. "
              "Filters apply before anything is counted, so every total and rate is made from the matches that are left."),
            p("**Dates select matches by start date, both ends included.** A match is in or out as a whole. A Test that starts on `date_to` brings in all its days, "
              "and a Test that started before `date_from` is out even if play carried on inside the range."),
            ul(["Team routes always need `gender`.",
                "An unknown parameter is a 400. A value that is not allowed is a 422.",
                "Filters combine: every one has to hold."]),
        ])),
        Section("paging", "Pagination", "Guides", "Pagination", "".join([
            p("A long list comes one of two ways, and each route is one or the other."),
            p("**Cursor paging** never drops a result. `limit` sets the page size. When there is more, the response has `next_cursor`. Pass it back as `cursor` with the same other parameters, and stop when it is `null`. "
              "A cursor is signed, belongs to one route and one data version, and is refused with a 400 if it was changed or is out of date."),
            table(["Cursor-paged route", "Default `limit`", "Largest `limit`"], [[f"`{path}`", dflt, top] for path, dflt, top in f["cursor_routes"]]),
            p("**Capped lists** have a `limit` and no cursor. They return the first `limit` results in order, and a longer list is cut. Narrow the request with `q` or a filter to reach the rest."),
            table(["Capped route", "Default `limit`", "Largest `limit`"], [[f"`{path}`", dflt, top] for path, dflt, top in f["capped_routes"]]),
            p("Matches come newest first. Splits and team groups come most matches first, then by key from A to Z. Other routes are short by nature."),
        ])),
        Section("statistics", "Statistics", "Guides", "Statistics rules", "".join([
            p("A rate is made from summed numerators and denominators, never from other rates. When there is nothing to divide by, the rate is `null`, not zero."),
            ul(["Economy is runs per six balls in every format. A batter's strike rate is runs per 100 balls faced. A bowler's is balls per wicket.",
                "A highest score or best figure is not additive. A group's is its own best, and the career's is the best of the groups.",
                "A wide is not a ball faced and a no ball is. Runs charged to a bowler are runs off the bat, wides and no balls. Byes, leg byes and penalty runs are not."]),
            p("**Leaderboards** need a sample before they rank a ratio. Any minimum can be set from 1 up, the minimums used come back in the response, and they are judged on the totals of the requested filters. "
              "There is no minimum of runs. The defaults are this API's own choice, not an official qualification rule."),
            table(["Ranking", "`min_balls`", "Other minimum"], [
                ["Batting average", d["batting average"][0], f"`min_dismissals` {d['batting average'][1]}"],
                ["Batting strike rate", d["batting strike rate"][0], "none"],
                ["Bowling average and strike rate", d["bowling average"][0], f"`min_wickets` {d['bowling average'][1]}"],
                ["Bowling economy", d["bowling economy"][0], "none"],
            ]),
            p("Leaderboards rank on the unrounded value and show it to two places. A tie goes to more balls, then more matches, then the smaller player id."),
        ])),
        Section("cricket", "Cricket rules", "Guides", "Cricket rules", dl([
            ("Super Overs", "A Super Over is a separate pair of innings that settles a tie. The scorecard and the balls of a match include it, and the match's innings list marks it with `is_super_over`. "
                            "Every total that adds up over matches leaves it out: career, season and split statistics, leaderboards, matchups and fielding. A player's match history still records the appearance, with `super_over`."),
            ("Results", "A team's record counts each match once as a win, loss, draw, tie or no result, and `matches` is their sum. A win includes Duckworth-Lewis and awarded wins. "
                        "A tie is neither a win nor a loss. When a Super Over or bowl-out settled it, Cricsheet names an eliminator winner, shown beside the tie as `ties_won_eliminator`, `ties_lost_eliminator` or `ties_no_eliminator`, "
                        "and these add up to `ties`. No result includes matches abandoned after play began. `decisive_win_percentage` is wins over wins plus losses, and `null` when there are no decisive matches. "
                        "The 2019 World Cup final, tied at 241 each, is a tie for England and for New Zealand, with England as the eliminator winner."),
            ("Dismissals", "A dismissal is any way a batter's innings ended except retired hurt and retired not out. A wicket credited to a bowler is only bowled, caught, caught and bowled, lbw, stumped or hit wicket. "
                           "A run out or a retirement out is a dismissal and not a bowler's wicket. A delivery with two wickets gives each batter a dismissal."),
            ("Overs", "`actual_overs` counts the legal balls bowled, so 146.1 is 146 overs and one ball. `scorecard_overs` is the umpires' count, from the over boundaries Cricsheet records, and is `null` when the source cannot settle it. "
                      "A set in The Hundred is five balls."),
            ("Maidens", "A maiden is an over confirmed complete, bowled whole by one bowler, with no run charged to them. An over shared by two bowlers, an unfinished last over, or one whose listed count the balls contradict is not a maiden."),
            ("Fielding", f"Fielding comes from Cricsheet's lists of fielders on each wicket, and the lists are incomplete. About {f['run_out_gap']} in 100 run outs have no fielder listed. "
                         "Run out involvements are therefore a floor. A catch is a caught wicket with the player as a named fielder. Caught and bowled has no fielder row, so it is counted apart. "
                         "A run out involvement goes to every named fielder once and is not a direct hit. A substitute fielder's credits are counted apart. In fielding groups, `matches` is the number of matches with a recorded credit."),
            ("Players", "A player is anyone Cricsheet names as one: a batter, bowler, squad member, named fielder, dismissed player, absent-hurt player or replacement. `/v1/players/{id}` gives the chosen name and the other spellings (aliases), "
                        "and `/v1/players?q=` searches all of them. A known player with no records inside the filters gets a 200 with zero totals and `null` rates. Only an unknown id is a 404."),
        ], css="terms")),
        Section("errors", "Errors", "Operations", "Errors", "".join([
            p("Every error is a problem document, `application/problem+json`, with `type`, `title`, `status`, `detail`, `instance` and a `request_id`, and `parameter` when one parameter is to blame. "
              "Errors say nothing about the server and are never cached."),
            table(["Status", "Meaning", "Common cause"], [
                ["400", "Bad request", "an unknown parameter, a changed or out-of-date cursor, an unknown field name"],
                ["404", "Not found", "an unknown match, player, team or route"],
                ["405", "Method not allowed", "anything but GET or OPTIONS"],
                ["413", "Request too large", "a request body, which this API never takes"],
                ["422", "Invalid parameter", "a value that is not allowed, a missing required parameter, a team given as its own opponent"],
                ["429", "Too many requests", "the rate limit. `Retry-After` says how many seconds to wait"],
                ["500", "Server error", "a fault. Quote the `request_id` when you ask about it"],
                ["503", "Busy", "the server is busy or a query was too slow. Narrow it with a filter and retry after `Retry-After`"],
            ]),
        ])),
        Section("limits", "Limits", "Operations", "Limits and retries", "".join([
            p(f"Each client has a token bucket that refills at {f['rate_per_minute']} tokens a minute and holds at most {f['rate_burst']}. The client is the address the request comes from, "
              f"and an IPv6 client is the network made of the first {f['ipv6_prefix']} bits of its address. A request costs tokens by how heavy it is, and one that finds too few gets a 429 with `Retry-After`."),
            table(["Cost in tokens", "Routes"], [["Not counted" if cost == 0 else str(cost), ", ".join(f"`{path}`" for path in paths)] for cost, paths in f["costs"]]),
            ul(["A response that counts against the limit carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`, unless a shared cache may store it. `/health` is never counted.",
                "Every answer except one a shared cache may store carries `X-Request-ID`. Quote it when you ask about a fault."]),
            f'<h3 id="busy">When the server is busy</h3>',
            p(f"A request that needs the database is admitted before it runs. At most {lim.units} run at once, in three classes. Heavy requests may use at most {lim.heavy_running_cap} of the units, "
              f"and heavy and medium together at most {lim.units - 1}, so a light request always has a unit. A request that cannot start waits in a small room for a fixed time, "
              "and one that finds the room full is refused at once. A client may have one heavy request running or waiting. These sizes are starting values, not measured capacity."),
            table(["Class", "Waiting room", "Longest wait", "Routes"], [[kind, str(lim.rooms[kind]), f"{lim.waits[kind]:g} s", ", ".join(f"`{t}`" for t, k in admission.TABLE.items() if k == kind)] for kind in classes]),
            p("Routes that need no database (" + ", ".join(f"`{t}`" for t, k in admission.TABLE.items() if k == admission.NONE and not t.startswith("/_via/")) + ") are not admitted and are served without a query."),
            table(["Answer", "Meaning", "What to do"], [
                ["429 problem document", "the rate limit, or a second heavy request from one client", "wait `Retry-After` seconds, then retry"],
                ["503 problem document", "the room for the route's class was full, or the wait ran out", "wait `Retry-After` seconds, then retry with a longer pause each time"],
                ["503 in plain text (`Service Unavailable`)", f"the server's own limit of {lim.connections} connections was reached. It has no `request_id` and none of this API's headers", "treat any 503 as a retry and do not rely on a body or header"]]),
            ul(["For a 429 or 503, wait at least `Retry-After` when it is given. Otherwise start at 1 second, double the wait each time with a random pause of up to half of it, stop doubling at 30 seconds, "
                "give up after a few tries, and never repeat the same request in parallel.",
                "A refused request still costs tokens, so a client that keeps asking a busy server empties its own allowance and then gets a 429."]),
        ])),
        Section("caching", "Caching", "Operations", "Caching", "".join([
            p("Every cacheable successful answer carries an `ETag` and a `Cache-Control` header. The ETag is made from the answer and the data version, so it changes when the data does and when anything in the request changes the answer. "
              "A different filter, field selection, page or cursor is a different cache entry."),
            table(["Kind", "Cache-Control", "Routes"], [[pol.name, caching.cache_control(pol), pol.for_routes] for pol in caching.POLICIES]),
            ul(["A cached copy is reused until its time is up, so after the data changes an answer can be up to an hour old, and a match answer up to a day.",
                "Send the ETag back as `If-None-Match`. An unchanged answer is a 304 with no body, and it carries the same `ETag` and `Cache-Control`. A `W/` prefix is ignored when comparing, and `*` is ignored.",
                "Errors, rate limit refusals, `/health` and `/status` are never stored: `Cache-Control: no-store`. `/health` and `/status` carry no `ETag`.",
                "Behind a shared cache, a cacheable answer carries none of `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `X-Request-ID` or `X-Response-Time-Ms`, because the cache would hand them to the next client. "
                "The licence `Link` and `X-Attribution` stay. A cached answer is not counted against your limit, because the server never sees the request."]),
        ])),
        Section("contact", "Contact", "About", "Developer & Contact", "".join([
            dl([
                ("Name", "Ari Adaikalam"),
                ("Portfolio", "[ariadaikalam.com](https://ariadaikalam.com)"),
                ("Email", "[ariadaikalam1234@gmail.com](mailto:ariadaikalam1234@gmail.com)"),
            ]),
            p("Bug reports, data discrepancies and suggestions are welcome. Please email them."),
            p("To make a report easy to follow, include the endpoint, the parameters you sent, what you expected and what you got, and the request ID if available: it is in the `request_id` of an error and in the `X-Request-ID` header, but a cached successful answer may carry none. "
              "Never send passwords, tokens or keys: this API has none, and nobody needs them to look into a report."),
        ])),
    ]
