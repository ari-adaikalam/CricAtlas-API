"""Fakes for Prometheus: a source that answers like the range query does, and a builder of a day of recorded samples with gaps, restarts and bursts."""
from cricket_notifier import rules
from cricket_notifier.prometheus import PrometheusUnavailable

INTERVAL = 15.0
ST = 1_700_000_000.0


class FakeSource:
    def __init__(self):
        self.series: dict[str, list] = {}
        self.down = False
        self.calls = []

    def matrix(self, name, end, range_seconds):
        self.calls.append((name, end, range_seconds))
        if self.down:
            raise PrometheusUnavailable("down")
        out = []
        for series in self.series.get(name, []):
            values = [(t, v) for t, v in series["values"] if end - range_seconds < t <= end]
            if values:
                out.append({"labels": dict(series["labels"]), "values": values})
        return out


def populate(source, t0, t1, *, per_step=None, outages=(), restarts=(), start=ST, memory=2_000_000_000, memory_limit=2_300_000_000, data_version="v1", built=None, traffic=True, uptime_from=None,
             statuses=None, refresh=None, versions=None):
    """Recorded series every 15 seconds from t0 to t1, all sharing their timestamps, as the rule group makes them.

    `per_step` is how much each counter goes up in a step ({'200': 8, '404': 1, '500': 1} by default, so 10 requests a step), `outages` are (from, to) windows with no samples, and `restarts` are the
    times the process restarts: its counters go back to zero and its start time changes."""
    per_step = {"200": 8, "404": 1, "500": 1} if per_step is None else per_step
    if not traffic:
        per_step = {}
    names = {name: [] for name in rules.names()}
    st = start
    counters = {"by_status": {k: 0 for k in per_step}, "faults": 0}
    totals = {"sum": 0, "cpu": 0.0}
    buckets = {"0.1": 0, "0.5": 0, "1.0": 0, "+Inf": 0}
    restart_times = sorted(restarts)
    t = t0
    n = 0
    series = {}
    while t <= t1:
        while restart_times and t >= restart_times[0]:
            restart_times.pop(0)
            st += 1000
            counters = {"by_status": {k: 0 for k in per_step}, "faults": 0}
            totals = {"sum": 0, "cpu": 0.0}
            buckets = {"0.1": 0, "0.5": 0, "1.0": 0, "+Inf": 0}
        for k, step in per_step.items():
            counters["by_status"][k] += step
        step_total = sum(per_step.values())
        totals["sum"] += step_total
        totals["cpu"] += 1.5
        if step_total:
            buckets["0.1"] += int(step_total * 0.6)
            buckets["0.5"] += int(step_total * 0.9)
            buckets["1.0"] += step_total
            buckets["+Inf"] += step_total
        in_outage = any(a < t < b for a, b in outages)
        if not in_outage:
            def add(name, labels, value):
                series.setdefault((name, tuple(sorted(labels.items()))), {"labels": labels, "values": []})["values"].append((t, value))
            add(rules.START, {}, st)
            add("cricket:requests:sum", {}, totals["sum"])
            for k, v in counters["by_status"].items():
                add("cricket:requests:by_status", {"status": k}, v)
            for le, v in buckets.items():
                if totals["sum"]:
                    add("cricket:request_duration:by_le", {"le": le}, v)
            for klass in ("light", "medium", "heavy"):
                add("cricket:admission:by_class_outcome", {"class": klass, "outcome": "refused_room"}, 0)
            add("cricket:faults:sum", {}, counters["faults"])
            add("cricket:faults_unwritten:sum", {}, 0)
            add("cricket:query_timeouts:sum", {}, 0)
            add("cricket:query_busy:sum", {}, 0)
            add("cricket:cpu_seconds:sum", {}, totals["cpu"])
            add("cricket:oom_kills:sum", {}, 0)
            add("cricket:memory_bytes:max", {}, memory + (n % 100) * 1_000_000)
            add("cricket:memory_limit_bytes:max", {}, memory_limit)
            add("cricket:uptime_seconds:max", {}, t - st)
            add("cricket:data_built:max", {}, built if built is not None else t0 - 3600)
            for version in ([data_version] if versions is None else [v for at, v in versions if at <= t][-1:] or [data_version]):
                add("cricket:data_version:info", {"data_version": version}, 1)
            if refresh is not None:        # what deploy/host/refresh_health.py writes: enabled, failed, pending, last check and last activation (epoch seconds), and when it last looked
                add("cricket:refresh_enabled:max", {}, refresh.get("enabled", 1))
                add("cricket:refresh_failed:max", {}, refresh.get("failed", 0) if not callable(refresh.get("failed")) else refresh["failed"](t))
                add("cricket:refresh_pending:max", {}, refresh.get("pending", 0))
                add("cricket:refresh_check:max", {}, refresh.get("check", 0))
                add("cricket:refresh_activation:max", {}, refresh.get("activation", 0))
                add("cricket:refresh_observed:max", {}, refresh["observed"] if "observed" in refresh else t)
        t += INTERVAL
        n += 1
    for (name, _), s in series.items():
        names[name].append(s)
    source.series = {k: v for k, v in names.items() if v}
    return source
