"""The daily summary: every figure for one period, with its completeness stated, as a plain context for the templates.

All figures come from Prometheus over exactly the period and are origin traffic only (cache hits are in Cloudflare's analytics). The arithmetic is in accounting.py: totals from raw samples, boundary
lag and gaps stated, a restart told by the process start time, latency undefined (not zero) when there were no requests, a gauge only ever "highest observed". A period with no samples at all says
"no data from <time> to <time>" and never shows zeros. A day with no traffic still produces a summary: it says zero requests.

Nothing a caller sent can be in it. It holds numbers, fixed words, the names of alerts (bounded and cut), and times.
"""
import json
import shutil
from datetime import datetime, timezone

from cricket_notifier import accounting as acc
from cricket_notifier import budget, refresh_results, rules
from cricket_notifier.config import Config
from cricket_notifier.ledger import Ledger, utc_day, utc_month
from cricket_notifier.prometheus import PrometheusUnavailable, TooManySamples
from cricket_notifier.schedule import Plan, describe, describe_length

INF = float("inf")
STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx", "aborted")
MAX_ALERT_NAMES = 10
MAX_GAP_NOTES = 10
HOUR = 3600
ROUTINE_NOTE = "Requests: counted from the sample at "      # said in every report, so it stays in the context but is not printed
REFRESH_STALE_CHECK = 36 * HOUR                             # the same threshold as the DataRefreshStale alert
REFRESH_STALE_OBSERVER = 1800


def human_bytes(n) -> str:
    if n is None:
        return "no data"
    for unit, size in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if abs(n) >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} B"


def count_text(value) -> str:
    return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"


def figure(result: acc.CounterResult, suffix: str = "") -> dict:
    """One total as the report shows it: the value, its marks, and a text that says 'at least' when a restart may have lost some."""
    if result.no_data or result.total is None:
        return {"value": None, "text": "no data", "marks": ["no_data"]}
    text = count_text(result.total) + suffix
    if result.may_undercount:
        text = "at least " + text
    return {"value": result.total, "text": text, "marks": result.marks}


def at(ts: float | None, zone) -> str:
    return "unknown" if ts is None else describe(datetime.fromtimestamp(ts, timezone.utc), zone)["local"]


def short_at(ts: float | None, zone, end: float) -> str:
    """A time for a row of the report, short enough for a phone: "Thu 29 Oct, 19:54". The zone and its offset are stated once for the section, and the year only when it is not the
    period's. A time whose offset differs from the one at the end of the period (a clock change) is shown in full so it cannot be misread."""
    if ts is None:
        return "unknown"
    local, last = datetime.fromtimestamp(ts, timezone.utc).astimezone(zone), datetime.fromtimestamp(end, timezone.utc).astimezone(zone)
    if local.utcoffset() != last.utcoffset():
        return at(ts, zone)
    year = f" {local.year}" if local.year != last.year else ""
    return f"{local:%a} {local.day} {local:%b}{year}, {local:%H:%M}"


def whole_sum(gaps) -> int | float:
    return acc.whole(sum(g.events for g in gaps))


def alert_name(text) -> str:
    return "".join(c for c in str(text)[:60] if c.isalnum() or c in "_-.: ")


class SummaryBuilder:
    def __init__(self, source, ledger: Ledger, config: Config, free_space=None):
        self.source, self.ledger, self.config = source, ledger, config
        self.free_space = free_space or (lambda path: shutil.disk_usage(path))

    # -- reading ---------------------------------------------------------------------------------------------------------------
    def _fetch(self, name: str, end: float, range_seconds: float) -> list:
        return self.source.matrix(name, end, range_seconds)

    def build(self, plan: Plan, now: float) -> dict:
        p = self.config.prometheus
        zone = self.config.schedule.zone
        start, end = plan.start.timestamp(), plan.slot.timestamp()
        gap = p.scrape_interval_seconds * p.max_boundary_lag_intervals
        window = (end - start) + p.lookback_hours * HOUR
        context = {"period": self._period(plan, zone), "notes": [], "prometheus_available": True}
        start_values: list = []
        try:
            start_series = self._fetch(rules.START, end, window)
            start_values = sorted(start_series[0]["values"]) if start_series else []
            data = self._prometheus_figures(start_values, start, end, gap, window, zone, context) if start_values else self._no_data()
        except (PrometheusUnavailable, TooManySamples) as error:
            context["prometheus_available"] = False
            context["notes"].append("Prometheus could not be read for this report" + (": too many samples" if isinstance(error, TooManySamples) else "") + ".")
            data = self._no_data()
        if context["prometheus_available"] and not start_values:
            context["notes"].append(f"No data from {at(start, zone)} to {at(end, zone)}: no samples of the API are stored for this period, so no figure below is a zero.")
        context.update(data)
        context.update(self._ledger_figures(plan, start, end, now, zone))
        context.update(self._bandwidth(start, end))
        context["disk"] = self._disk()
        context["incomplete"] = self._incomplete(context)
        context["refresh"] = self._refresh_or_unknown(start, end, window, zone, context["prometheus_available"])
        context["attention"] = self._attention(context)
        context["shown_notes"] = [note for note in context["notes"] if not note.startswith(ROUTINE_NOTE)]
        return context

    def _refresh_or_unknown(self, start: float, end: float, window: float, zone, available: bool) -> dict:
        if not available:
            return {"state": "unknown", "headline": "The refresh could not be read because monitoring could not be read.", "checked": None, "new_data": None}
        try:
            info = self._fetch("cricket:data_version:info", end, window)
            seen = sorted(((series["values"][0][0], series["labels"].get("data_version")) for series in info if any(start < t <= end for t, _ in series["values"])), key=lambda x: x[0])
            versions = [v for _, v in seen if v]
            before = [(series["values"][-1][0], series["labels"].get("data_version")) for series in info if series["values"][-1][0] <= start]
            earlier = max(before)[1] if before else None
            change = (earlier or versions[0], versions[-1]) if versions and (earlier or versions[0]) != versions[-1] else None
            return self._refresh(end, window, start, zone, change, self._names())
        except (PrometheusUnavailable, TooManySamples):
            return {"state": "unknown", "headline": "The refresh could not be read because monitoring could not be read.", "checked": None, "new_data": None}

    def fallback_context(self, plan: Plan, now: float, why: str) -> dict:
        """What is sent when the report itself could not be built: the period, a plain statement that nothing below is a measurement, and whatever of the ledger, bandwidth and disk can still be read."""
        zone = self.config.schedule.zone
        context = {"period": self._period(plan, zone), "notes": [f"The report could not be built ({why}). No figure below is a measurement, and none is a zero."], "prometheus_available": False, **self._no_data()}
        for build in (lambda: self._ledger_figures(plan, plan.start.timestamp(), plan.slot.timestamp(), now, zone), lambda: self._bandwidth(plan.start.timestamp(), plan.slot.timestamp())):
            try:
                context.update(build())
            except Exception:
                context["notes"].append("Part of the notifier's own figures could not be read either.")
        context["disk"] = self._disk()
        context["incomplete"] = True
        context["refresh"] = {"state": "unknown", "headline": "The refresh could not be read because the report itself could not be built.", "checked": None, "new_data": None}
        context["attention"] = ["The report could not be built. No figure in it is a measurement, and none is a zero."]
        context["shown_notes"] = list(context["notes"])
        return context

    @staticmethod
    def _incomplete(context: dict) -> bool:
        """Whether any figure in the report is incomplete, may undercount, has no data, or could not be read at all: the subject says so, so it is seen before the message is opened."""
        if not context["prometheus_available"]:
            return True
        bad = {"incomplete", "may_undercount", "restart_unknown", "no_data"}

        def walk(node):
            if isinstance(node, dict):
                if "marks" in node and bad & set(node["marks"]):
                    return True
                return any(walk(v) for v in node.values())
            return False
        return walk({k: context[k] for k in ("requests", "latency", "admission", "faults", "resources") if k in context})

    def _period(self, plan: Plan, zone) -> dict:
        s, e = describe(plan.start, zone), describe(plan.slot, zone)
        local = plan.slot.astimezone(zone)
        period = {"start": s, "end": e, "length": describe_length(plan.length_hours), "start_kind": plan.start_kind, "first_run_note": None, "end_date": f"{local:%a} {local.day} {local:%b %Y}"}
        if plan.start_kind == "first_run":
            period["first_run_note"] = "This is the first report: its period starts when monitoring first ran, not at a scheduled boundary."
        return period

    @staticmethod
    def _no_data() -> dict:
        nothing = {"value": None, "text": "no data", "marks": ["no_data"]}
        return {"requests": {"total": nothing, "by_status": {c: nothing for c in STATUS_CLASSES}, "server_errors": nothing, "rate_limited": nothing},
                "latency": {"text": "no data", "plain": "no data", "p50": None, "p95": None, "marks": ["no_data"], "undefined": False, "few": False},
                "admission": {c: nothing for c in ("light", "medium", "heavy")}, "faults": {"total": nothing, "unwritten": nothing, "query_timeouts": nothing, "query_busy": nothing},
                "resources": {"memory_peak": nothing, "memory_limit": nothing, "memory_percent": None, "cpu_seconds": nothing, "oom_kills": nothing, "restarts": nothing, "uptime": nothing},
                "data": {"version": None, "name": None, "label": "unknown", "built": None, "age": None}, "coverage": None}

    def _prometheus_figures(self, start_values: list, start: float, end: float, gap: float, window: float, zone, context: dict) -> dict:
        if not start_values:
            return self._no_data()
        notes = context["notes"]
        times = [t for t, _ in start_values]

        def fetch(name):
            return self._fetch(name, end, window)

        def single(name) -> acc.CounterResult:
            series = fetch(name)
            if not series:
                return acc.CounterResult(None, no_data=True)
            values = sorted(acc.aggregate(series)[()])
            return acc.counter_window(acc.attach_start(values, start_values), start, end, gap)

        def grouped(name, key) -> dict:
            """Recorded sums by label. A group that was not present at a scrape counted nothing then, so it is 0 at that time (a counter a group has not used has no series)."""
            out = {}
            for k, values in acc.aggregate(fetch(name), key=key).items():
                lookup = dict(values)
                aligned = [(t, lookup.get(t, 0.0)) for t in times]
                out[k] = acc.counter_window(acc.attach_start(aligned, start_values), start, end, gap)
            return out

        requests = single("cricket:requests:sum")
        notes.extend(self._explain("Requests", requests, start, end, gap, zone))
        by_status = grouped("cricket:requests:by_status", lambda l: l.get("status", "other"))

        def status_class(label):
            return "aborted" if label == "aborted" else (label[0] + "xx" if label[:1].isdigit() else "other")
        classes = {c: 0 for c in STATUS_CLASSES}
        exact = {}
        flags = {c: {"incomplete": False, "may_undercount": False} for c in STATUS_CLASSES}
        for label, result in by_status.items():
            cls = status_class(label)
            if cls in classes and result.total is not None:
                classes[cls] += result.total
                flags[cls]["incomplete"] |= result.incomplete
                flags[cls]["may_undercount"] |= result.may_undercount
            exact[label] = result
        status_figures = {}
        for cls in STATUS_CLASSES:                                                  # the API is up (there are start-time samples), so a status that never occurred is 0, not missing
            status_figures[cls] = figure(acc.CounterResult(classes[cls], incomplete=flags[cls]["incomplete"], may_undercount=flags[cls]["may_undercount"]))
        five = acc.CounterResult(classes["5xx"], incomplete=flags["5xx"]["incomplete"], may_undercount=flags["5xx"]["may_undercount"])
        limited = exact.get("429", acc.CounterResult(0))
        buckets = {}
        for le, result in grouped("cricket:request_duration:by_le", lambda l: l.get("le", "")).items():
            try:
                buckets[INF if le in ("+Inf", "Inf") else float(le)] = result
            except ValueError:
                continue
        if buckets:
            latency = acc.latency_from(buckets)
        elif requests.total == 0:
            latency = acc.Latency(0, None, None, undefined=True, few=False, marks=())      # no request was timed because there was none: undefined, not zero
        else:
            latency = acc.latency_from({})
        admission = {c: acc.CounterResult(0) for c in ("light", "medium", "heavy")}
        refused = {c: [0, False, False] for c in admission}
        for (klass, outcome), result in grouped("cricket:admission:by_class_outcome", lambda l: (l.get("class", ""), l.get("outcome", ""))).items():
            if klass in refused and outcome.startswith("refused") and result.total is not None:
                refused[klass][0] += result.total
                refused[klass][1] |= result.incomplete
                refused[klass][2] |= result.may_undercount
        admission_figures = {c: figure(acc.CounterResult(v[0], incomplete=v[1], may_undercount=v[2])) for c, v in refused.items()}
        memory = acc.highest_observed(sorted(v for s in fetch("cricket:memory_bytes:max") for v in s["values"]), start, end)
        limit_series = fetch("cricket:memory_limit_bytes:max")
        limit = acc.last_value(sorted(limit_series[0]["values"]), end) if limit_series else None
        memory_text = {"value": memory[0], "text": human_bytes(memory[0]) + (" (highest observed)" if memory[0] is not None else ""), "marks": ["highest_observed"] if memory[0] is not None else ["no_data"]}
        restarts, can_tell = acc.restarts_in(start_values, start, end)
        uptime_series = fetch("cricket:uptime_seconds:max")
        uptime = acc.last_value(sorted(uptime_series[0]["values"]), end) if uptime_series else None
        built_series = fetch("cricket:data_built:max")
        built = acc.last_value(sorted(built_series[0]["values"]), end) if built_series else None
        info = fetch("cricket:data_version:info")
        version = None
        if info:
            latest = max(info, key=lambda s: s["values"][-1][0] if s["values"] else 0)
            version = latest["labels"].get("data_version")
        coverage = acc.coverage(start_values, start, end, self.config.prometheus.scrape_interval_seconds)
        if restarts:
            notes.append(f"The API restarted {restarts} time{'s' if restarts != 1 else ''} in this period. Events between the last sample before a restart and the restart itself are not counted, so totals across it may undercount.")
        nothing = {"value": None, "text": "no data", "marks": ["no_data"]}
        names = self._names()
        return {
            "requests": {"total": figure(requests), "by_status": status_figures, "server_errors": figure(five), "rate_limited": figure(limited)},
            "latency": self._latency_text(latency),
            "admission": admission_figures,
            "faults": {"total": figure(single("cricket:faults:sum")), "unwritten": figure(single("cricket:faults_unwritten:sum")), "query_timeouts": figure(single("cricket:query_timeouts:sum")),
                       "query_busy": figure(single("cricket:query_busy:sum"))},
            "resources": {"memory_peak": memory_text, "memory_limit": {"value": limit, "text": human_bytes(limit) if limit else "no limit reported", "marks": [] if limit else ["no_data"]},
                          "memory_percent": round(100 * memory[0] / limit, 1) if memory[0] is not None and limit else None, "cpu_seconds": figure(single("cricket:cpu_seconds:sum"), " s"),
                          "oom_kills": figure(single("cricket:oom_kills:sum")),
                          "restarts": {"value": restarts, "text": str(restarts) if can_tell else "no data", "marks": [] if can_tell else ["no_data"]},
                          "uptime": {"value": uptime, "text": f"{uptime / 3600:.1f} h" if uptime is not None else "no data", "marks": [] if uptime is not None else ["no_data"]}},
            "data": {"version": version, "name": names.get(version) if version else None, "label": ((refresh_results.data_text(names.get(version), version) if names.get(version) else f"version {version}") if version else "unknown"),
                     "built": short_at(built, zone, end) if built else None, "age": f"{(end - built) / 3600:.1f} h" if built else None},
            "coverage": None if coverage is None else round(100 * coverage, 1),
        }

    def _names(self) -> dict:
        """The friendly names the refresh recorded for data versions, from the result records the host writes (version -> "Data of 2026-10-28" or a milestone label). A version with no
        name here is shown by its hash alone. The notifier never works a name out, and a folder it cannot read just means no names."""
        directory = self.config.refresh.results_dir
        if not directory:
            return {}
        try:
            records, _ = refresh_results.read_records(directory, 40)
        except Exception:
            return {}
        names = {}
        for _, record in sorted(records, key=lambda item: item[0]):
            for version, name in ((record.get("previous_version"), record.get("previous_display_name")), (record.get("served_version"), record.get("served_display_name"))):
                if version and name and name != version:
                    names[version] = name
        return names

    def _refresh(self, end: float, window: float, start: float, zone, version_change: tuple | None, names: dict | None = None) -> dict:
        """What the daily refresh did in this period, in plain words. Every figure is a gauge the host writes (deploy/host/refresh_health.py). No series at all means the
        refresh is not installed, and the report says that and not that it worked."""
        def samples(name):
            return sorted(v for series in self._fetch(name, end, window) for v in series["values"])
        enabled = samples("cricket:refresh_enabled:max")
        if not enabled:
            return {"state": "off", "headline": "The automatic daily refresh is not installed.", "checked": None, "new_data": None}
        if acc.last_value(enabled, end) == 0:
            return {"state": "off", "headline": "The automatic daily refresh is switched off.", "checked": None, "new_data": None}
        failed, pending = samples("cricket:refresh_failed:max"), samples("cricket:refresh_pending:max")
        checked, activated, observed = (acc.last_value(samples(n), end) for n in ("cricket:refresh_check:max", "cricket:refresh_activation:max", "cricket:refresh_observed:max"))
        failed_now, pending_now = acc.last_value(failed, end) == 1, acc.last_value(pending, end) == 1
        failed_before = any(v == 1 for t, v in failed if start < t <= end)
        checked = checked or None                                                             # 0 means never
        activated = activated or None
        checked_here = bool(checked and start < checked <= end)
        activated_here = bool(activated and start < activated <= end)
        names = names or {}
        # A check that came after the period's publication belongs to a later attempt. It may still be running or may have found nothing, and either way it had not published anything
        # when this report was made, so neither the headline nor the rows may say it published.
        later_check = bool(activated_here and checked and checked > activated)
        if not checked:
            checked_text = "No successful check of Cricsheet has been recorded yet."
        elif later_check:
            checked_text = f"Latest check of Cricsheet: {short_at(checked, zone, end)}. It came after that publication and had not published anything when this report was made."
        else:
            checked_text = f"Last successful check of Cricsheet: {short_at(checked, zone, end)}."
        if activated_here:
            if version_change:
                before, after = (refresh_results.data_text(names.get(v), v) for v in version_change)
                named = before != version_change[0] or after != version_change[1]
                moved = f" {before} was replaced by {after}." if named else f" Version {before} was replaced by {after}."
            else:
                moved = ""
            when = "earlier in this period, at" if later_check else "at"
            new_text = f"New data was published {when} {short_at(activated, zone, end)}.{moved}"
        elif activated:
            new_text = f"No new data in this period. The data now live was published {short_at(activated, zone, end)}."
        else:
            new_text = "No new data in this period. The refresh has not published anything yet."
        if failed_now:
            state, headline = "attention", "The refresh failed. The data being served was not changed. Read the server's journal before trying again."
        elif pending_now:
            state, headline = "attention", "New data is live, but clearing the cache or the public check has not finished."
        elif observed is None or end - observed > REFRESH_STALE_OBSERVER:
            state, headline = "attention", "The refresh is not being observed: its status was not updated in the last 30 minutes."
        elif not checked or end - checked > REFRESH_STALE_CHECK:
            state, headline = "attention", "The source has not been checked successfully for more than 36 hours."
        elif failed_before:
            state, headline = "ok", "Worked, after a failure earlier in this period."
        elif later_check:
            state, headline = "ok", "New data was published earlier in this period. A newer check had not published anything yet."
        elif activated_here:
            state, headline = "ok", "Checked Cricsheet and published new data."
        elif checked_here:
            state, headline = "ok", "Checked Cricsheet. Nothing new, so nothing changed."
        else:
            state, headline = "ok", "No check in this period yet, but the last one is recent."
        return {"state": state, "headline": headline, "checked": checked_text, "new_data": new_text, "times_note": self._times_note(end, zone)}

    @staticmethod
    def _times_note(end: float, zone) -> str:
        local = datetime.fromtimestamp(end, timezone.utc).astimezone(zone)
        offset = local.strftime("%z")
        return f"Times in this section are {zone.key} time ({offset[:3]}:{offset[3:]})."

    def _attention(self, context: dict) -> list:
        """The few things worth reading first, in plain words. Empty means nothing needs attention."""
        reasons = []
        if not context["prometheus_available"]:
            reasons.append("Monitoring could not be read, so the traffic and server figures are missing.")
        refresh = context.get("refresh") or {}
        if refresh.get("state") == "attention":
            reasons.append(refresh["headline"])
        errors = context["requests"]["server_errors"]["value"]
        if errors:
            reasons.append(f"{count_text(errors)} server errors (5xx).")
        resources = context["resources"]
        if (resources["oom_kills"]["value"] or 0) > 0:
            reasons.append("The server was killed for running out of memory.")
        if (resources["restarts"]["value"] or 0) > 0:
            reasons.append(f"The API restarted {resources['restarts']['value']} time{'s' if resources['restarts']['value'] != 1 else ''}.")
        if context["alerts"]["fired"]:
            reasons.append(f"{context['alerts']['fired']} alert{'s' if context['alerts']['fired'] != 1 else ''} fired.")
        email = context["email"]
        if email["failed"] or email["unconfirmed"] or email["unreviewed_failed"] or email["unreviewed_unconfirmed"]:
            reasons.append("Some email was not delivered or is unconfirmed. Review it with the notifier's status command.")
        if email["breaker"] != "closed":
            reasons.append(f"The email circuit breaker is {email['breaker']}, so alerts may not be sent.")
        if email["storage"] != "ok":
            reasons.append(f"The email ledger storage is {email['storage']}.")
        if context["disk"]["free_percent"] is not None and context["disk"]["free_percent"] < 20:
            reasons.append(f"Disk is {context['disk']['free_percent']} percent free.")
        if context.get("incomplete") and context["prometheus_available"]:
            reasons.append("Some figures are incomplete. The notes at the end say which and why.")
        return reasons

    @staticmethod
    def _latency_text(latency: acc.Latency) -> dict:
        if "no_data" in latency.marks:
            text = "no data"
        elif latency.undefined:
            text = "no requests, latency undefined"
        else:
            text = f"p50 {latency.p50 * 1000:.0f} ms, p95 {latency.p95 * 1000:.0f} ms" if latency.p50 is not None and latency.p95 is not None else "no data"
            if latency.top_open:
                text += " (a quantile is above the highest bucket, so it is only a lower bound)"
            if latency.few:
                text += f" (only {int(latency.count)} requests: too few to mean much)"
            text += " (an estimate by interpolation, accurate to the bucket width)"
        if "no_data" in latency.marks:
            plain = "no data"
        elif latency.undefined:
            plain = "no requests, so no response time"
        elif latency.p50 is None or latency.p95 is None:
            plain = "no data"
        else:
            plain = f"half of requests took under {latency.p50 * 1000:.0f} ms, 95 percent under {latency.p95 * 1000:.0f} ms (an estimate)"
            if latency.top_open:
                plain += ", and the slowest were above the highest bucket"
            if latency.few:
                plain += f". Only {int(latency.count)} requests, too few to mean much"
        return {"text": text, "plain": plain, "p50": latency.p50, "p95": latency.p95, "marks": list(latency.marks), "undefined": latency.undefined, "few": latency.few}

    def _explain(self, label: str, r: acc.CounterResult, start: float, end: float, gap: float, zone) -> list:
        """What a reader needs to know about the edges and gaps of one headline total."""
        if r.no_data:
            return []
        notes = [f"{label}: counted from the sample at {at(r.start_sample, zone)} to the sample at {at(r.end_sample, zone)} (the boundaries are {at(start, zone)} and {at(end, zone)}; lag {r.start_lag:+.0f} s and {r.end_lag:+.0f} s)."]
        if r.starts_late:
            notes.append(f"{label}: no sample exists at or before the start boundary, so the first sample after it was used, {-r.start_lag:.0f} s late.")
        if r.start_lag is not None and r.start_lag > gap:
            notes.append(f"{label}: the sample used for the start boundary is {r.start_lag:.0f} s old (over the {gap:.0f} s limit), so events after it and before the boundary are counted in this report, in the report before this one they were not.")
        if r.end_lag is not None and r.end_lag > gap:
            notes.append(f"{label}: the count stops at the sample at {at(r.end_sample, zone)}, {r.end_lag:.0f} s before the boundary. Events after it are counted in the next report.")
        for g in r.gaps[:MAX_GAP_NOTES]:
            before = " and some happened before the start boundary" if g.start < start else ""
            notes.append(f"{label}: {count_text(g.events)} events happened somewhere between {at(g.start, zone)} and {at(g.end, zone)}, when monitoring has no samples. They are counted in this report{before}.")
            if g.restart:
                notes.append(f"{label}: the API restarted inside that gap, so the events before the restart that fall in it are not counted.")
            if g.restart_unknown:
                notes.append(f"{label}: the process start time is missing across that gap, so a restart inside it cannot be ruled out.")
        if len(r.gaps) > MAX_GAP_NOTES:
            rest = r.gaps[MAX_GAP_NOTES:]
            notes.append(f"{label}: and {len(rest)} more gaps, with {count_text(whole_sum(rest))} events between them, which are counted in this report too.")
        return notes

    # -- the ledger, bandwidth and disk ----------------------------------------------------------------------------------------------
    def _ledger_figures(self, plan: Plan, start: float, end: float, now: float, zone) -> dict:
        L = self.ledger
        usage = L.usage(end)
        day, month = budget.Limits.day(self.config.budgets), budget.Limits.month(self.config.budgets)

        def total(u, limits):
            return {"used": sum(u.values()), "cap": limits.summary + limits.pool}
        counts = {s: L.read_one("SELECT COUNT(*) FROM messages WHERE state=? AND updated_at>? AND updated_at<=? AND kind NOT IN ('summary')", (s, start, end))[0]
                  for s in ("accepted", "failed", "unconfirmed", "budget_suppressed", "expired")}
        accepted_summaries = L.read_one("SELECT COUNT(*) FROM messages WHERE state='accepted' AND kind='summary' AND accepted_at>? AND accepted_at<=?", (start, end))[0]
        unreviewed = {s: L.read_one("SELECT COUNT(*) FROM messages WHERE state=?", (s,))[0] for s in ("failed", "unconfirmed")}
        fired = L.read("SELECT context FROM messages WHERE kind='alert' AND created_at>? AND created_at<=?", (start, end))
        recovered = L.read("SELECT context FROM messages WHERE kind='recovery' AND created_at>? AND created_at<=?", (start, end))

        def names(rows):
            seen = {}
            for row in rows:
                try:
                    stored = json.loads(row[0] or "{}")
                    name = alert_name(stored.get("group_name") or stored.get("alertname") or "unnamed")      # the webhook stores `group_name`
                except (ValueError, TypeError, AttributeError):
                    name = "unnamed"
                seen[name] = seen.get(name, 0) + 1
            return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ALERT_NAMES]
        skipped = L.read("SELECT slot FROM messages WHERE kind='summary' AND state='skipped' AND due_at>? ORDER BY due_at", (end - 7 * 86400,))
        breaker = L.breaker()
        observed = L.get_meta("observed", {})
        status = L.storage_status()
        return {"alerts": {"fired": len(fired), "recovered": len(recovered), "fired_names": names(fired), "recovered_names": names(recovered)},
                "email": {"accepted": counts["accepted"] + accepted_summaries, "accepted_note": "accepted by Resend, delivery not tracked", "failed": counts["failed"], "unconfirmed": counts["unconfirmed"],
                          "budget_suppressed": counts["budget_suppressed"], "expired": counts["expired"], "unreviewed_failed": unreviewed["failed"], "unreviewed_unconfirmed": unreviewed["unconfirmed"],
                          "day_used": total(usage["day"], day), "month_used": total(usage["month"], month),
                          "account_day": observed.get("daily_used") if observed.get("day") == utc_day(end) else None,
                          "account_month": observed.get("monthly_used") if observed.get("month") == utc_month(end) else None,
                          "breaker": breaker.get("state", "closed"), "storage": status.mode, "skipped_slots": [describe(datetime.strptime(r[0].replace("summary/", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), zone)["local"]
                                                                                                         for r in skipped if r[0] and r[0].startswith("summary/")]}}

    def _bandwidth(self, start: float, end: float) -> dict:
        first, last = -(-int(start) // HOUR), int(end) // HOUR
        month_start = datetime.fromtimestamp(end, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = [(r[0], r[1], r[2]) for r in self.ledger.read("SELECT hour, bytes, measured FROM bandwidth")]
        period = acc.sum_bandwidth(rows, first, last)
        month = acc.sum_bandwidth(rows, int(month_start.timestamp()) // HOUR, last)

        def text(r):
            if r["hours"] == 0 or r["unmeasured_hours"] == r["hours"]:
                return "no data"                                           # nothing was measured, which is not zero bytes
            base = human_bytes(r["bytes"])
            return (f"at least {base}, with {r['unmeasured_hours']} of {r['hours']} hours unmeasured" if r["unmeasured_hours"] else base)
        return {"bandwidth": {"period": {**period, "text": text(period)}, "month": {**month, "text": text(month)}, "allowance": "500 GB",                                                  # the provider counts decimal gigabytes, and the alerts and the dashboard say 500 GB too
                              "note": "Bytes sent on the public interface, counted by whole UTC hours. They include more than API responses and may not match how the provider counts."}}

    def _disk(self) -> dict:
        try:
            usage = self.free_space(self.config.disk.path)
            return {"free_percent": round(100 * usage.free / usage.total, 1), "free": human_bytes(usage.free), "total": human_bytes(usage.total)}
        except OSError:
            return {"free_percent": None, "free": "no data", "total": "no data"}
