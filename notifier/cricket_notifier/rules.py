"""The Prometheus recording rules the summary reads, and the queries it makes, from one table, so the two cannot drift apart.

The summary needs raw samples with their timestamps (a gap is a gap in those timestamps). The API's counters have a series for every route, method and status, so reading them raw would be thousands
of series a day. Instead a recording rule sums them (by status, by class and outcome, by histogram bucket) every scrape interval, all in one rule group, so every recorded sample of one evaluation shares
one timestamp, and the process start time is recorded in the same group, so it can be matched to a sample by that timestamp.

A counter that has not been used has no series, and `sum()` of nothing is nothing, which would look like no data. So each counter is recorded as `sum(x) or 0 * max(<the API's start time>)`: it is 0 when the API is up and
has not counted anything, and absent only when the API is not being scraped, which is the one case that is no data.

The notifier fetches the recorded series by name. `render_yaml()` writes the same table as the rule file Prometheus loads, and a test holds the committed file to it.
"""
from dataclasses import dataclass

GROUP = "cricket_notifier_summary"
START = "cricket:process_start_time:max"


@dataclass(frozen=True)
class Rule:
    name: str
    source: str            # what it is made from
    kind: str              # counter, gauge or info
    expr: str


def _up(api_job: str) -> str:
    return f'max(process_start_time_seconds{{job="{api_job}"}})'


def rules(api_job: str = "cricket-api") -> tuple[Rule, ...]:
    start = _up(api_job)

    def counter(name: str, metric: str, by: str = "") -> Rule:
        grouped = f"sum by ({by}) ({metric})" if by else f"sum({metric})"
        # with a grouping the zero fallback is not made: the groups that exist are the ones that were used, and the sum of the groups is what the summary adds up
        expr = f"{grouped} or 0 * {start}" if not by else f"{grouped}"
        return Rule(name, metric, "counter", expr)
    return (
        Rule(START, "process_start_time_seconds", "gauge", start),
        counter("cricket:requests:sum", "cricket_api_requests_total"),
        counter("cricket:requests:by_status", "cricket_api_requests_total", "status"),
        counter("cricket:admission:by_class_outcome", "cricket_api_admission_total", "class, outcome"),
        counter("cricket:request_duration:by_le", "cricket_api_request_duration_seconds_bucket", "le"),
        counter("cricket:faults:sum", "cricket_api_faults_total"),
        counter("cricket:faults_unwritten:sum", "cricket_api_fault_log_suppressed_total"),
        counter("cricket:query_timeouts:sum", "cricket_api_query_timeouts_total"),
        counter("cricket:query_busy:sum", "cricket_api_query_busy_total"),
        counter("cricket:cpu_seconds:sum", "cricket_api_cgroup_cpu_seconds_total"),
        counter("cricket:oom_kills:sum", "cricket_api_cgroup_oom_kills_total"),
        Rule("cricket:memory_bytes:max", "cricket_api_cgroup_memory_bytes", "gauge", "max(cricket_api_cgroup_memory_bytes)"),
        Rule("cricket:memory_limit_bytes:max", "cricket_api_cgroup_memory_limit_bytes", "gauge", "max(cricket_api_cgroup_memory_limit_bytes)"),
        Rule("cricket:uptime_seconds:max", "cricket_api_uptime_seconds", "gauge", "max(cricket_api_uptime_seconds)"),
        Rule("cricket:data_built:max", "cricket_api_data_built_timestamp_seconds", "gauge", "max(cricket_api_data_built_timestamp_seconds)"),
        Rule("cricket:data_version:info", "cricket_api_data_info", "info", "max by (data_version) (cricket_api_data_info)"),
        # The daily refresh, written by deploy/host/refresh_health.py through node_exporter. Absent when the refresh is not installed, which the summary says plainly.
        Rule("cricket:refresh_enabled:max", "cricket_data_refresh_enabled", "gauge", "max(cricket_data_refresh_enabled)"),
        Rule("cricket:refresh_failed:max", "cricket_data_refresh_failed", "gauge", "max(cricket_data_refresh_failed)"),
        Rule("cricket:refresh_pending:max", "cricket_data_refresh_pending", "gauge", "max(cricket_data_refresh_pending)"),
        Rule("cricket:refresh_check:max", "cricket_data_refresh_last_check_seconds", "gauge", "max(cricket_data_refresh_last_check_seconds)"),
        Rule("cricket:refresh_activation:max", "cricket_data_refresh_last_activation_seconds", "gauge", "max(cricket_data_refresh_last_activation_seconds)"),
        Rule("cricket:refresh_observed:max", "cricket_data_refresh_observed_seconds", "gauge", "max(cricket_data_refresh_observed_seconds)"),
    )


def names(api_job: str = "cricket-api") -> set[str]:
    return {r.name for r in rules(api_job)}


def source_metrics(api_job: str = "cricket-api") -> set[str]:
    """The metrics of the API that the rules read, which the API's own tests check are still exposed."""
    return {r.source for r in rules(api_job) if r.source.startswith("cricket_api_")}


def render_yaml(api_job: str = "cricket-api", interval: str = "15s") -> str:
    lines = ["# Generated by `python -m cricket_notifier rules`. Do not edit: the table is in cricket_notifier/rules.py.", "groups:", f"  - name: {GROUP}", f"    interval: {interval}", "    rules:"]
    for r in rules(api_job):
        lines += [f"      - record: {r.name}", f"        expr: {r.expr}"]
    return "\n".join(lines) + "\n"
