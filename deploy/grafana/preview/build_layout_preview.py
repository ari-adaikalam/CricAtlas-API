#!/usr/bin/env python3
"""The approved layout of the Cricket API dashboard, and a way to look at it with sample data before anything is deployed.

    build_layout_preview.py            writes the two sample-data files used for the screenshots (not committed, never provisioned)
    build_layout_preview.py --check    exits 1 if the provisioned dashboard (../dashboards/cricket-api.json) differs from the layout this builds

../build_dashboard.py calls build() here to write the provisioned dashboard. The layout reuses every panel and query of the original arrangement (build_panels() there stays the source of them) and changes the layout:
  * an answer-first row ("Right now"): six tiles that say whether the API is up, whether anything is firing, how slow it is, how busy, how much is being refused and how full its memory is
  * the same charts, grouped as Traffic, Capacity and Host and disk, in that order
  * the notifier and the monthly traffic sections folded away until opened
  * fewer, larger tiles: duplicate information (requests in flight, the table of alerts) is dropped from the first screen
Four of the six tiles are new (three new queries, the latency tile reuses the chart's p95 query). They use only metrics that already exist. Their colour thresholds are provisional, like the alert thresholds they sit beside.

Sample data: the screenshots use Grafana's built-in TestData data source with numbers shaped like the soak run on the real host, so a layout can be judged without a server. The files written for it
(preview-*.json) are not committed and are never provisioned.
"""
import copy
import importlib.util
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("build_dashboard", HERE.parent / "build_dashboard.py")
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)

PROVISIONED = HERE.parent / "dashboards" / "cricket-api.json"
TD = {"type": "grafana-testdata-datasource", "uid": "preview-testdata"}


def stat_opts(graph: str = "none", text: str = "auto", color: str = "value") -> dict:
    return {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "textMode": text, "colorMode": color, "graphMode": graph, "justifyMode": "auto"}


def build() -> dict:
    cur = bd.build_panels()
    by_title = {p["title"]: p for p in cur["panels"] if p["type"] != "row" and not (p["title"] == "Alerts firing" and p["type"] == "table")}
    alerts_table = [p for p in cur["panels"] if p["title"] == "Alerts firing" and p["type"] == "table"][0]
    bd._id[0] = max(p["id"] for p in cur["panels"])
    target, panel, row = bd.target, bd.panel, bd.row
    out = []
    y = 0

    def place(title: str, x: int, y: int, w: int, h: int, new_title: str | None = None) -> None:
        p = copy.deepcopy(by_title[title])
        p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        if new_title:
            p["title"] = new_title
        out.append(p)

    biz = 'route=~"/v1/.+",route!="/v1/meta"'      # business traffic, as in build_dashboard.py: health probes are left out
    traffic = f"sum(rate(cricket_api_requests_total{{{biz}}}[5m])) > 0"
    out.append(row("Right now", y)); y += 1
    place("API up", 0, y, 4, 5, "API")
    place("Alerts firing", 4, y, 4, 5, "Alerts")
    out.append(panel("stat", "Latency", [target(f'histogram_quantile(0.95, sum by (le) (rate(cricket_api_request_duration_seconds_bucket{{{biz}}}[5m]))) and on() ({traffic})', "p95", "A")], 8, y, 4, 5,
                     "Latency, 95th percentile: the slowest 5 percent of requests took at least this long, over the last 5 minutes. Estimated from the histogram buckets. With no requests it is undefined and shows no data.", unit="s",
                     steps=[("green", None), ("orange", 1), ("red", 3)], options=stat_opts("none")))
    out.append(panel("stat", "Requests", [target(f"sum(rate(cricket_api_requests_total{{{biz}}}[5m]))", "requests", "A")], 12, y, 4, 5,
                     "Requests a second that reached the origin, over the last 5 minutes. Cloudflare cache hits never get here.", unit="reqps", options=stat_opts("none")))
    out.append(panel("stat", "Refused", [target('(sum(rate(cricket_api_admission_total{outcome=~"refused_room|refused_wait|refused_client"}[5m])) or vector(0)) + (sum(rate(cricket_api_requests_total{status="429"}[5m])) or vector(0))', "refused", "A")],
                     16, y, 4, 5, "Refused a second. Requests turned away: admission refusals (a class was full) plus per-client rate limit refusals (429).", unit="reqps",
                     steps=[("green", None), ("orange", 0.1), ("red", 1)], options=stat_opts("none")))
    out.append(panel("stat", "Memory", [target('max(process_resident_memory_bytes{job="cricket-api"}) / max(cricket_api_cgroup_memory_limit_bytes)', "of the limit", "A")], 20, y, 4, 5,
                     "Memory in use: the API process's memory as a share of its container limit. Page cache is left out because the kernel can reclaim it.", unit="percentunit",
                     steps=[("green", None), ("orange", 0.7), ("red", 0.85)], extra_defaults={"min": 0, "max": 1}, options=stat_opts("none")))
    y += 5
    table = copy.deepcopy(alerts_table)
    table["gridPos"] = {"x": 0, "y": y, "w": 24, "h": 3}
    table["title"] = "Alerts firing now"
    table["fieldConfig"]["defaults"]["noValue"] = "No alerts firing"
    out.append(table)
    y += 3

    out.append(row("Traffic and refusals", y)); y += 1
    place("Requests by status class", 0, y, 12, 8)
    place("Request latency", 12, y, 12, 8)
    y += 8
    place("Admission: refusals and waiting, by class", 0, y, 12, 8)
    place("Rate-limited (429) and faults", 12, y, 12, 8)
    y += 8

    out.append(row("Capacity", y)); y += 1
    place("API container memory against its limit", 0, y, 12, 8)
    place("API CPU", 12, y, 12, 8)
    y += 8
    place("Uptime", 0, y, 8, 3)
    place("Data age", 8, y, 8, 3)
    place("Data version", 16, y, 8, 3)
    y += 3

    out.append(row("Host and disk", y)); y += 1
    place("Disk used, by filesystem", 0, y, 12, 8)
    place("Free space, inodes and forecast", 12, y, 12, 8)
    y += 8
    for i, (t, short) in enumerate((("Inode use (/)", "Inodes"), ("Disk growth, 7 days (/)", "Growth, 7 d"), ("Journal size", "Journal"), ("Docker images", "Images"), ("Prometheus blocks + WAL", "Prometheus"), ("Disk scan age", "Scan age"))):
        place(t, i * 4, y, 4, 4, short)
    y += 4
    place("Each part against its budget", 0, y, 24, 8)
    y += 8

    folded = []
    for title, titles in (("Outbound traffic this month", ("Outbound traffic this month (500 GB allowance)", "Traffic this month, and hours not measured", "Outbound rate on the public interface")),
                          ("Email (the notifier)", ("Messages by state", "Send budgets used", "Account usage reported by Resend", "Failed or unconfirmed", "Summary overdue", "Oldest past its deadline",
                                                    "Time since the last accepted message", "Circuit breaker", "Ledger storage"))):
        r = row(title, y)
        r["collapsed"] = True
        yy = y + 1
        kids = []
        for t in titles:
            p = copy.deepcopy(by_title[t])
            g = p["gridPos"]
            kids.append(p)
        # keep each section's own internal arrangement, moved to sit under its row
        top = min(k["gridPos"]["y"] for k in kids)
        for k in kids:
            k["gridPos"]["y"] = yy + (k["gridPos"]["y"] - top)
        r["panels"] = kids
        folded.append(r)
        y += 1
    out.extend(folded)

    d = copy.deepcopy(cur)
    d["panels"] = out
    d["description"] = "Private monitoring of the Cricket API. Read-only, provisioned from the repository."
    d["time"] = {"from": "now-6h", "to": "now"}
    # Memory: a browser asks for one point per pixel (up to about 1,000 a series) and refreshes every minute. 500 points and a 5 minute refresh lowered Grafana's unreclaimable memory from 433 to 313 MiB at the
    # first open of this dashboard in a 512 MiB cgroup (measured 2026-10-14, docs/measured_results.md), after the kernel killed Grafana at 455 MiB on the server.
    d["refresh"] = "5m"
    for panel_ in [x for p_ in out for x in [p_, *p_.get("panels", [])]]:
        if panel_["type"] != "row":
            panel_["maxDataPoints"] = 500
    d["version"] = 1
    return d


# Sample data -------------------------------------------------------------------------------------------------------------------------------------

LABELS = {"class": ["light", "medium", "heavy"], "component": ["docker_images", "journal", "prometheus", "scratch", "caddy_logs", "ledger", "grafana", "api_data"], "device": ["ens3"], "period": ["day", "month"],
          "klass": ["alert", "summary"], "state": ["accepted", "queued", "failed"], "data_version": ["3cb4dded36cd"], "mountpoint": ["/"]}
UNIT_BASE = {"reqps": 1.0, "s": 0.4, "bytes": 2e8, "percent": 25.0, "percentunit": 0.3, "Bps": 3e4, "ops": 0.03, "short": 1.0}
# (base, relative noise) by panel title and legend, for the panels where a believable shape matters. Shaped like the soak run: about 3.3 requests a second, p95 near 0.7 s, API memory near 300 MiB of 2,200.
SHAPES = {
    "up": (1, 0), "firing": (0, 0), "requests": (3.2, .08), "refused": (.003, .8), "of the limit": (.13, .05),
    "2xx": (3.1, .08), "3xx": (.15, .3), "4xx": (.06, .6), "5xx": (0, 0), "aborted": (0, 0),
    "p50": (.03, .15), "p95": (.7, .25), "p99": (1.7, .3),
    "container (with page cache)": (3.3e8, .08), "peak": (1.05e9, 0), "limit": (2.2e9, 0), "process resident": (2.8e8, .06),
    "CPU seconds a second": (.38, .2), "throttled": (0, 0),
    "host disk (/)": (24, .01), "scratch volume": (2, .1), "ledger volume": (1.5, .05), "Prometheus volume": (9, .03),
    "free bytes (/)": (2.9e10, .002), "free in 24 h at the 6 h rate": (2.88e10, .004), "free in 7 d at the 24 h rate": (2.8e10, .01),
    "429": (.02, .8), "faults": (0, 0), "query timeouts": (0, 0), "query slots busy": (0, 0),
}
TITLE_LAST = {"API": 1, "API up": 1, "Alerts": 0, "Alerts firing": 0, "Latency": .7, "Requests": 3.3, "Refused": .002, "Memory": .13, "Uptime": 55000, "Data age": 4.2 * 86400, "Requests in flight": 3,
              "Inode use (/)": 6, "Disk growth, 7 days (/)": 2.1e8, "Journal size": 1.4e8, "Docker images": 1.9e9, "Prometheus blocks + WAL": 1.1e8, "Disk scan age": 310,
              "Failed or unconfirmed": 0, "Summary overdue": 0, "Oldest past its deadline": 0, "Time since the last accepted message": 9 * 3600, "Circuit breaker": 0, "Ledger storage": 1,
              "Outbound traffic this month (500 GB allowance)": .04, "Traffic this month, and hours not measured": 2e10}
COMPONENT_SHARE = {"docker_images": .48, "journal": .35, "prometheus": .06, "scratch": .02, "caddy_logs": .09, "ledger": .03, "grafana": .12, "api_data": .31}


def series(base: float, noise: float, seed: str, n: int = 72, last: float | None = None) -> str:
    """A smooth, slowly wandering series (an exponentially smoothed random walk) so the layout is judged on its shape and not on jagged sample noise."""
    rnd = random.Random(seed)
    x, vals = 0.0, []
    for _ in range(n):
        x = 0.9 * x + 0.1 * (rnd.random() * 2 - 1) * 3
        vals.append(max(0.0, base * (1 + noise * max(-1.0, min(1.0, x)))) if noise else base)
    if last is not None:
        vals[-1] = last
    return ",".join(f"{v:.6g}" for v in vals)


def sample_targets(p: dict) -> list:
    out = []
    unit = p["fieldConfig"]["defaults"].get("unit", "short")
    last = TITLE_LAST.get(p["title"])
    for t in p["targets"]:
        legend = t.get("legendFormat", "") or p["title"]
        label = re.search(r"\{\{(\w+)\}\}", legend)
        names = [legend]
        if label:
            names = [legend.replace(label.group(0), v) for v in LABELS.get(label.group(1), ["a", "b"])]
        for name in names:
            if p["title"] == "Each part against its budget":
                comp = name
                csv = series(COMPONENT_SHARE.get(comp, .1), 0, f"{p['id']}{name}")
            else:
                base, noise = SHAPES.get(name, (UNIT_BASE.get(unit, 1.0) * (0.0 if "refused" in name or name in ("waiting light", "waiting medium", "waiting heavy") else 1.0), 0.15))
                if name.startswith("running"):
                    base, noise = 0.4, 0.5
                if name.startswith("refused"):
                    base, noise = (0.0, 0) if name.endswith("light") else ((0.02, 0.6) if name.endswith("medium") else (0.08, 0.7))
                if p["title"] == "Send budgets used":
                    base, noise = 0.08, 0.2
                if p["title"] == "Messages by state":
                    base, noise = {"accepted": 14, "queued": 0, "failed": 0}.get(name, 0), 0
                csv = series(base, noise, f"{p['id']}{name}{t['refId']}", last=last if len(p["targets"]) == 1 and last is not None else None)
            out.append({"refId": f"{t['refId']}{len(out)}", "datasource": TD, "scenarioId": "csv_metric_values", "stringInput": csv, "alias": name})
    return out


def sampled(d: dict, title: str) -> dict:
    d = copy.deepcopy(d)
    d["title"] = title
    d["uid"] = "preview-" + ("current" if "Previous" in title else "new")
    d["refresh"] = ""
    todo = list(d["panels"])
    for p in list(todo):
        if p["type"] == "row" and p.get("panels"):
            todo.extend(p["panels"])
    for p in todo:
        if p["type"] == "row":
            continue
        p["datasource"] = TD
        if p["type"] == "table":
            p["targets"] = [{"refId": "A", "datasource": TD, "scenarioId": "csv_content", "csvContent": "alertname,severity,since\n"}]
        else:
            p["targets"] = sample_targets(p)
    return d


def main() -> int:
    text = json.dumps(build(), indent=2) + "\n"
    if "--check" in sys.argv:
        return 0 if PROVISIONED.exists() and PROVISIONED.read_text() == text else 1
    (HERE / "preview-new.json").write_text(json.dumps(sampled(build(), "Cricket API (New layout, sample data)"), indent=2))
    (HERE / "preview-current.json").write_text(json.dumps(sampled(bd.build_panels(), "Cricket API (Previous layout, sample data)"), indent=2))
    n = len([p for p in build()["panels"] if p["type"] != "row"]) + sum(len(p["panels"]) for p in build()["panels"] if p["type"] == "row")
    print(f"wrote the two sample-data files: {n} panels in the new layout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
