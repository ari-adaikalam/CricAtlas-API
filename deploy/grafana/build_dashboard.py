#!/usr/bin/env python3
"""Write dashboards/cricket-api.json. The dashboard is generated so that its queries sit next to the reasoning for them, and a test holds the committed JSON equal to what this prints.

Rules the panels follow (operations proposal, section 6):
  * a series with no samples is drawn as missing, never as 0 (spanNulls off, "no data" text on stat panels)
  * latency with no requests is undefined: its queries return nothing when the request rate is 0
  * the email section shows "time since the last accepted message" as information only, because the one message expected on an ordinary day is the daily summary
  * nothing here can change anything: the dashboard is read-only and has no links out
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "cricket-api.json"
DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]
_y = [0]


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False, fmt: str = "time_series") -> dict:
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "format": fmt}
    if instant:
        t["instant"] = True
        t["range"] = False
    return t


def thresholds(steps: list[tuple]) -> dict:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def panel(kind: str, title: str, targets: list, x: int, y: int, w: int, h: int, description: str = "", unit: str = "short", steps=None, extra_defaults=None, options=None, overrides=None) -> dict:
    _id[0] += 1
    defaults = {"unit": unit, "noValue": "no data", "color": {"mode": "palette-classic" if kind == "timeseries" else "thresholds"}, "thresholds": thresholds(steps or [("green", None)])}
    if kind == "timeseries":
        defaults["custom"] = {"spanNulls": False, "lineWidth": 1, "fillOpacity": 10, "showPoints": "never", "drawStyle": "line", "thresholdsStyle": {"mode": "off"}}
    defaults.update(extra_defaults or {})
    return {"id": _id[0], "type": kind, "title": title, "description": description, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {}}


def row(title: str, y: int) -> dict:
    _id[0] += 1
    return {"id": _id[0], "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def build_panels() -> dict:
    """The panels and queries in their original order, the source of every panel the dashboard shows. build() arranges them into the approved layout."""
    _id[0] = 0
    p = []
    y = 0
    legend = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}}
    stat = lambda: {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "textMode": "auto", "colorMode": "value", "graphMode": "none"}
    mappings = lambda m: {"mappings": [{"type": "value", "options": m}]}

    p.append(row("Service", y)); y += 1
    p.append(panel("stat", "API up", [target('up{job="cricket-api"}', "up", instant=True)], 0, y, 4, 4, "1 when Prometheus can scrape the API's metrics listener, 0 when it cannot, no data when there is no target.", steps=[("red", None), ("green", 1)],
                   extra_defaults=mappings({"0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}), options=stat()))
    p.append(panel("stat", "Uptime", [target("max(cricket_api_uptime_seconds)", "uptime", instant=True)], 4, y, 4, 4, "Time since the API process started. A short value means a recent restart.", unit="s", options=stat()))
    p.append(panel("stat", "Alerts firing", [target('count(ALERTS{alertstate="firing"}) or vector(0)', "firing", instant=True)], 8, y, 4, 4, "Alerts that are firing now. The table at the bottom of the page lists them.",
                   steps=[("green", None), ("red", 1)], options=stat()))
    p.append(panel("stat", "Data version", [target("cricket_api_data_info", "{{data_version}}", instant=True)], 12, y, 4, 4, "The data version of the files being served.", options={**stat(), "textMode": "name"}))
    p.append(panel("stat", "Data age", [target("time() - max(cricket_api_data_built_timestamp_seconds)", "age", instant=True)], 16, y, 4, 4, "Time since the served files were built. The alert threshold is provisional.", unit="s",
                   steps=[("green", None), ("orange", 14 * 86400), ("red", 21 * 86400)], options=stat()))
    p.append(panel("stat", "Requests in flight", [target("max(cricket_api_requests_in_flight)", "in flight", instant=True)], 20, y, 4, 4, "Requests being handled right now.", options=stat()))
    y += 4
    p.append(panel("timeseries", "Requests by status class", [
        target('sum(rate(cricket_api_requests_total{status=~"2..|2xx",route=~"/v1/.+",route!="/v1/meta"}[5m]))', "2xx", "A"), target('sum(rate(cricket_api_requests_total{status=~"3..|3xx",route=~"/v1/.+",route!="/v1/meta"}[5m]))', "3xx", "B"),
        target('sum(rate(cricket_api_requests_total{status=~"4..|4xx",route=~"/v1/.+",route!="/v1/meta"}[5m]))', "4xx", "C"), target('sum(rate(cricket_api_requests_total{status=~"5..|5xx",route=~"/v1/.+",route!="/v1/meta"}[5m]))', "5xx", "D"),
        target('sum(rate(cricket_api_requests_total{status="aborted",route=~"/v1/.+",route!="/v1/meta"}[5m]))', "aborted", "E")], 0, y, 12, 8,
        "Business requests a second (the /v1 routes, health and status probes left out) that reached the origin. Cloudflare cache hits never get here, so this is not the traffic the site sees.", unit="reqps",
        extra_defaults={"custom": {"spanNulls": False, "lineWidth": 1, "fillOpacity": 20, "showPoints": "never", "drawStyle": "line", "stacking": {"mode": "normal"}, "thresholdsStyle": {"mode": "off"}}},
        overrides=[{"matcher": {"id": "byName", "options": "5xx"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
                   {"matcher": {"id": "byName", "options": "4xx"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}], options=legend))
    BIZ = 'route=~"/v1/.+",route!="/v1/meta"'      # business traffic: the /v1 routes without /v1/meta, so health probes do not dilute the shares and percentiles
    traffic = f"sum(rate(cricket_api_requests_total{{{BIZ}}}[5m])) > 0"
    p.append(panel("timeseries", "Request latency", [
        target(f"histogram_quantile(0.50, sum by (le) (rate(cricket_api_request_duration_seconds_bucket{{{BIZ}}}[5m]))) and on() ({traffic})", "p50", "A"),
        target(f"histogram_quantile(0.95, sum by (le) (rate(cricket_api_request_duration_seconds_bucket{{{BIZ}}}[5m]))) and on() ({traffic})", "p95", "B"),
        target(f"histogram_quantile(0.99, sum by (le) (rate(cricket_api_request_duration_seconds_bucket{{{BIZ}}}[5m]))) and on() ({traffic})", "p99", "C")], 12, y, 12, 8,
        "Business requests only (health probes left out so they do not pull the percentiles down). Estimated from the histogram buckets, so a value is only as precise as the bucket it falls in. With no requests the latency is undefined and the line is absent, not 0.", unit="s", options=legend))
    y += 8
    p.append(panel("timeseries", "Admission: refusals and waiting, by class", [
        target('sum by (class) (rate(cricket_api_admission_total{outcome=~"refused_room|refused_wait|refused_client"}[5m]))', "refused {{class}}", "A"),
        target("sum by (class) (cricket_api_admission_waiting)", "waiting {{class}}", "B"), target("sum by (class) (cricket_api_admission_running)", "running {{class}}", "C")], 0, y, 12, 8,
        "Refusals a second (rooms full, wait ran out, a second heavy request from one client), and how many are waiting or running now, for each of the light, medium and heavy classes.", options=legend))
    p.append(panel("timeseries", "Rate-limited (429) and faults", [
        target('sum(rate(cricket_api_requests_total{status="429"}[5m]))', "429", "A"), target("sum(rate(cricket_api_faults_total[5m]))", "faults", "B"),
        target("sum(rate(cricket_api_query_timeouts_total[5m]))", "query timeouts", "C"), target("sum(rate(cricket_api_query_busy_total[5m]))", "query slots busy", "D")], 12, y, 12, 8,
        "Per-client rate limit refusals, unexpected faults (each is also in the fault log), queries stopped by the time limit, and queries refused because every slot stayed taken.", unit="ops", options=legend))
    y += 8
    p.append(panel("timeseries", "API container memory against its limit", [
        target("max(cricket_api_cgroup_memory_bytes)", "container (with page cache)", "A"), target("max(cricket_api_cgroup_memory_peak_bytes)", "peak", "B"),
        target("max(cricket_api_cgroup_memory_limit_bytes)", "limit", "C"), target("max(process_resident_memory_bytes{job=\"cricket-api\"})", "process resident", "D")], 0, y, 12, 8,
        "Container memory includes page cache, which the kernel can reclaim, so compare it with the process resident line before acting on a high value.", unit="bytes", options=legend))
    p.append(panel("timeseries", "API CPU", [
        target("sum(rate(cricket_api_cgroup_cpu_seconds_total[5m]))", "CPU seconds a second", "A"), target("sum(rate(cricket_api_cgroup_cpu_throttled_seconds_total[5m]))", "throttled", "B")], 12, y, 12, 8,
        "CPU the container used, and time it was held back by a CPU limit (the host has 2 vCPUs).", options=legend))
    y += 8

    p.append(row("Disk and the host", y)); y += 1
    used = lambda mp: f'100 * (1 - node_filesystem_avail_bytes{{mountpoint={mp}}} / node_filesystem_size_bytes{{mountpoint={mp}}})'
    p.append(panel("timeseries", "Disk used, by filesystem", [target(used('"/"'), "host disk (/)", "A"), target(used('"/srv/cricket/scratch"'), "scratch volume", "B"), target(used('"/srv/cricket/ledger"'), "ledger volume", "C"),
                                                              target(used('"/srv/cricket/prometheus"'), "Prometheus volume", "D")], 0, y, 12, 8,
        "Percent used. The host disk's alert lines are at 55, 70 and 85. A fixed-size volume that fills stops its owner and nothing else.", unit="percent",
        steps=[("green", None), ("yellow", 55), ("orange", 70), ("red", 85)],
        extra_defaults={"min": 0, "max": 100, "custom": {"spanNulls": False, "lineWidth": 1, "fillOpacity": 5, "showPoints": "never", "drawStyle": "line", "thresholdsStyle": {"mode": "line"}}}, options=legend))
    p.append(panel("timeseries", "Free space, inodes and forecast", [
        target('node_filesystem_avail_bytes{mountpoint="/"}', "free bytes (/)", "A"), target('predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[6h], 24 * 3600)', "free in 24 h at the 6 h rate", "B"),
        target('predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[24h], 7 * 86400)', "free in 7 d at the 24 h rate", "C")], 12, y, 12, 8,
        "Free bytes on the host disk and where the last 6 and 24 hours' trend would take them. A line below zero is the alert condition.", unit="bytes", options=legend))
    y += 8
    p.append(panel("stat", "Inode use (/)", [target('100 * (1 - node_filesystem_files_free{mountpoint="/"} / node_filesystem_files{mountpoint="/"})', "inodes", instant=True)], 0, y, 4, 4, "Percent of inodes used on the host disk. Alert at 80.", unit="percent",
                   steps=[("green", None), ("orange", 70), ("red", 80)], options=stat()))
    p.append(panel("stat", "Disk growth, 7 days (/)", [target('-delta(node_filesystem_avail_bytes{mountpoint="/"}[7d])', "growth", instant=True)], 4, y, 4, 4, "How much more of the host disk is used than 7 days ago. Needs 7 days of data.", unit="bytes", options=stat()))
    p.append(panel("stat", "Journal size", [target('cricket_disk_component_bytes{component="journal"}', "journal", instant=True)], 8, y, 4, 4, "System journal size, from the disk scan. Limit 400 MB.", unit="bytes", options=stat()))
    p.append(panel("stat", "Docker images", [target('cricket_disk_component_bytes{component="docker_images"}', "images", instant=True)], 12, y, 4, 4, "Docker image size, from the disk scan. Budget 4 GB.", unit="bytes", options=stat()))
    p.append(panel("stat", "Prometheus blocks + WAL", [target("max(prometheus_tsdb_storage_blocks_bytes) + max(prometheus_tsdb_wal_storage_size_bytes)", "TSDB", instant=True)], 16, y, 4, 4,
                   "Prometheus's own data against its 1.5 GB limit (on a 2 GB volume).", unit="bytes", steps=[("green", None), ("orange", 1.2e9), ("red", 1.5e9)], options=stat()))
    p.append(panel("stat", "Disk scan age", [target("time() - max(cricket_disk_scan_timestamp_seconds)", "age", instant=True)], 20, y, 4, 4, "Time since the disk scan last wrote its metrics. Alert at 30 minutes. No data means it has never run.", unit="s",
                   steps=[("green", None), ("orange", 900), ("red", 1800)], options=stat()))
    y += 4
    p.append(panel("bargauge", "Each part against its budget", [target("cricket_disk_component_bytes / cricket_disk_component_budget_bytes", "{{component}}", instant=True)], 0, y, 24, 9,
                   "Each component's size as a share of its row in the disk budget. Over 1 (100 percent) for an hour is an alert. A component with no scan data is missing, not 0.", unit="percentunit",
                   steps=[("green", None), ("orange", 0.8), ("red", 1)], extra_defaults={"min": 0, "max": 1.5}, options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                                                                                                                  "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}))
    y += 9

    p.append(row("Traffic and data", y)); y += 1
    p.append(panel("gauge", "Outbound traffic this month (500 GB allowance)", [target("cricket_notifier_bandwidth_month_bytes / 500e9", "share", instant=True)], 0, y, 6, 8,
                   "At least this share of the 500 GB monthly allowance, from the notifier's hourly record of the public interface (UTC calendar month). It includes SSH, updates and mail calls, and may not match how OVH counts.",
                   unit="percentunit", steps=[("green", None), ("yellow", 0.5), ("orange", 0.8), ("red", 0.95)], extra_defaults={"min": 0, "max": 1}, options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}))
    p.append(panel("stat", "Traffic this month, and hours not measured", [target("cricket_notifier_bandwidth_month_bytes", "sent", "A", instant=True), target("cricket_notifier_bandwidth_month_unmeasured_hours", "unmeasured hours", "B", instant=True)],
                   6, y, 6, 8, "Bytes counted so far this month, and the whole hours that the notifier could not measure. A month figure with unmeasured hours is 'at least'.", unit="bytes",
                   overrides=[{"matcher": {"id": "byName", "options": "unmeasured hours"}, "properties": [{"id": "unit", "value": "short"}]}], options={**stat(), "textMode": "value_and_name"}))
    p.append(panel("timeseries", "Outbound rate on the public interface", [target('rate(node_network_transmit_bytes_total{job="node", device!~"lo|docker.*|br-.*|veth.*"}[5m])', "{{device}}", "A")], 12, y, 12, 8,
                   "Transmit rate by interface. The notifier's configured bandwidth device is the public one.", unit="Bps", options=legend))
    y += 8

    p.append(row("Email (the notifier)", y)); y += 1
    p.append(panel("bargauge", "Messages by state", [target("sum by (state) (cricket_notifier_messages) > 0", "{{state}}", instant=True)], 0, y, 8, 8,
                   "Messages in the ledger by state. 'accepted' means Resend returned a message id: delivery is not tracked.", options={"orientation": "horizontal", "displayMode": "basic", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}))
    p.append(panel("bargauge", "Send budgets used", [target("cricket_notifier_budget_used / cricket_notifier_budget_limit", "{{period}} {{klass}}", instant=True)], 8, y, 8, 8,
                   "What the notifier has charged in the current UTC day and month, as a share of what each class may use. Summaries have a reserve, and the suppression, email-path and host notices the last slots.",
                   unit="percentunit", steps=[("green", None), ("orange", 0.8), ("red", 1)], extra_defaults={"min": 0, "max": 1}, options={"orientation": "horizontal", "displayMode": "gradient", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}))
    p.append(panel("stat", "Account usage reported by Resend", [target("cricket_notifier_account_used", "{{period}}", instant=True)], 16, y, 8, 8,
                   "What Resend last reported for the whole account in the current UTC day and month (the free plan allows 100 a day and 3,000 a month). The gap from the notifier's own use is mail from somewhere else. No data when none is known for the period.",
                   options={**stat(), "textMode": "value_and_name"}))
    y += 8
    p.append(panel("stat", "Failed or unconfirmed", [target("max(cricket_notifier_failed_or_unconfirmed)", "to review", instant=True)], 0, y, 4, 4, "Messages that failed or are unconfirmed and not yet reviewed.", steps=[("green", None), ("orange", 1)], options=stat()))
    p.append(panel("stat", "Summary overdue", [target("max(cricket_notifier_summary_overdue_seconds)", "overdue", instant=True)], 4, y, 4, 4, "How far past its grace period a due summary is. Zero when none is due.", unit="s", steps=[("green", None), ("red", 1)], options=stat()))
    p.append(panel("stat", "Oldest past its deadline", [target("max(cricket_notifier_oldest_overshoot_seconds)", "overshoot", instant=True)], 8, y, 4, 4, "How far the most overdue unsent message is past its deadline.", unit="s", steps=[("green", None), ("red", 1)], options=stat()))
    p.append(panel("stat", "Time since the last accepted message", [target("time() - max(cricket_notifier_last_accepted_timestamp_seconds > 0)", "since", instant=True)], 12, y, 4, 4,
                   "For information only, not an alert: a quiet day has just the daily summary, so a day of silence is normal. No data when nothing is in the ledger.", unit="s", options=stat()))
    p.append(panel("stat", "Circuit breaker", [target('max(cricket_notifier_breaker_state{state="open"}) + 2 * max(cricket_notifier_breaker_state{state="half_open"})', "state", instant=True)], 16, y, 4, 4,
                   "Closed is normal. Open means sending failed three times in a row and alert mail is being held.", extra_defaults=mappings({"0": {"text": "closed", "color": "green"}, "1": {"text": "OPEN", "color": "red"}, "2": {"text": "probing", "color": "orange"}}),
                   steps=[("green", None), ("red", 1)], options=stat()))
    p.append(panel("stat", "Ledger storage", [target('max(cricket_notifier_storage_mode{mode="ok"}) + 2 * max(cricket_notifier_storage_mode{mode="wal_over_target"}) + 3 * max(cricket_notifier_storage_mode{mode="low_priority_refused"}) + 4 * max(cricket_notifier_storage_mode{mode="full"})', "mode", instant=True)], 20, y, 4, 4,
                   "The ledger's storage guard. Anything but ok means alert webhooks may be refused (Alertmanager retries).",
                   extra_defaults=mappings({"1": {"text": "ok", "color": "green"}, "2": {"text": "wal over target", "color": "yellow"}, "3": {"text": "REFUSING", "color": "red"}, "4": {"text": "FULL", "color": "red"}}), steps=[("green", None)], options=stat()))
    y += 4
    p.append(panel("table", "Alerts firing", [target('ALERTS{alertstate="firing"}', "", instant=True, fmt="table")], 0, y, 24, 8,
                   "Every alert that is firing now, from Prometheus.", options={"showHeader": True, "cellHeight": "sm"},
                   extra_defaults={"custom": {"align": "auto"}},
                   overrides=[]))
    return {"uid": "cricket-api", "title": "Cricket API", "description": "Private monitoring of the Cricket API. Read-only, provisioned from the repository.", "tags": ["cricket"], "timezone": "browser", "editable": False,
            "graphTooltip": 1, "refresh": "1m", "schemaVersion": 41, "version": 1, "time": {"from": "now-24h", "to": "now"}, "timepicker": {}, "templating": {"list": []}, "annotations": {"list": []},
            "links": [], "panels": p}


def build() -> dict:
    """The provisioned dashboard: the same panels and queries, in the approved answer-first layout (preview/build_layout_preview.py holds the arrangement and the sample-data previews)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_layout_preview", Path(__file__).resolve().parent / "preview" / "build_layout_preview.py")
    layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout)
    return layout.build()


def main() -> int:
    text = json.dumps(build(), indent=2) + "\n"
    if "--check" in sys.argv:
        return 0 if OUT.exists() and OUT.read_text() == text else 1
    OUT.write_text(text)
    print(f"wrote {OUT.name}: {len(build()['panels'])} panels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
