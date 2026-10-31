"""The daily summary's figures: a busy day, a day with no traffic, gaps, restarts, a gap over the boundary, no Prometheus, and the sections that come from the ledger, bandwidth and disk."""
import json
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from cricket_notifier import schedule as sc
from cricket_notifier.schedule import Plan
from cricket_notifier.summary import SummaryBuilder

from fakes import FakeSource, populate
from test_ledger_storage import seed

Disk = namedtuple("Disk", "total used free")


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


START, END = utc(2026, 10, 9, 10), utc(2026, 10, 10, 10)          # 9pm Sydney to 9pm Sydney, 24 hours
S, E = START.timestamp(), END.timestamp()


def plan(start=START, end=END, kind="boundary"):
    return Plan(slot=end, start=start, start_kind=kind, skip=False)


@pytest.fixture
def rig(make_ledger, clock):
    class Rig:
        pass
    r = Rig()
    r.ledger, r.source, r.clock = make_ledger(), FakeSource(), clock
    r.builder = SummaryBuilder(r.source, r.ledger, r.ledger.config, free_space=lambda path: Disk(100 * 10 ** 9, 40 * 10 ** 9, 60 * 10 ** 9))
    r.build = lambda p=None, now=None: r.builder.build(p or plan(), now or E + 60)
    return r


def day(rig, **kw):
    populate(rig.source, S - 3600, E + 600, **kw)
    return rig


# A busy day -----------------------------------------------------------------------------------------------------------------

def test_a_busy_day_has_exact_totals_and_no_marks(rig):
    c = day(rig).build()
    r = c["requests"]
    assert r["total"] == {"value": 57_600, "text": "57,600", "marks": []}
    assert [r["by_status"][k]["text"] for k in ("2xx", "3xx", "4xx", "5xx", "aborted")] == ["46,080", "0", "5,760", "5,760", "0"]
    assert r["server_errors"]["text"] == "5,760" and r["rate_limited"]["text"] == "0" and c["coverage"] == 100.0
    assert c["prometheus_available"] is True and c["period"]["length"] == "24 h" and c["period"]["start_kind"] == "boundary" and c["period"]["first_run_note"] is None


def test_the_boundary_samples_are_shown_in_a_note(rig):
    c = day(rig).build()
    assert len(c["notes"]) == 1 and c["notes"][0].startswith("Requests: counted from the sample at ") and "lag +0 s and +0 s" in c["notes"][0]
    assert "Sat 10 Oct 2026, 21:00 +11:00 (Australia/Sydney)" in c["notes"][0]


def test_latency_is_an_estimate_by_interpolation_and_says_so(rig):
    lat = day(rig).build()["latency"]
    assert lat["p50"] == pytest.approx(0.1 * 28_800 / 34_560) and lat["p95"] == pytest.approx(0.75)
    assert lat["text"].startswith("p50 83 ms, p95 750 ms") and "interpolation" in lat["text"] and lat["marks"] == [] and lat["undefined"] is False and lat["few"] is False


def test_resources_data_and_the_rest(rig):
    c = day(rig, built=S - 7200).build()
    assert c["resources"]["memory_peak"]["text"] == "1.95 GiB (highest observed)" and c["resources"]["memory_peak"]["marks"] == ["highest_observed"]
    assert c["resources"]["memory_limit"]["text"] == "2.14 GiB" and c["resources"]["memory_percent"] == 91.3
    assert c["resources"]["cpu_seconds"]["text"] == "8,640 s" and c["resources"]["restarts"]["text"] == "0" and c["resources"]["oom_kills"]["text"] == "0"
    assert c["data"]["version"] == "v1" and c["data"]["age"] == "26.0 h" and c["data"]["built"] == "Fri 9 Oct, 19:00" and c["data"]["label"] == "version v1"
    assert c["faults"]["total"]["text"] == "0" and c["faults"]["unwritten"]["text"] == "0" and c["admission"]["heavy"]["text"] == "0"


def test_no_memory_limit_is_stated_not_assumed(rig):
    day(rig)
    del rig.source.series["cricket:memory_limit_bytes:max"]
    c = rig.build()
    assert c["resources"]["memory_limit"]["text"] == "no limit reported" and c["resources"]["memory_percent"] is None


# No traffic ---------------------------------------------------------------------------------------------------------------------

def test_a_day_with_no_traffic_is_zero_requests_and_undefined_latency_not_zero_latency(rig):
    c = day(rig, traffic=False).build()
    assert c["requests"]["total"] == {"value": 0, "text": "0", "marks": []}
    assert all(f["text"] == "0" and f["marks"] == [] for f in c["requests"]["by_status"].values())
    assert c["latency"]["text"] == "no requests, latency undefined" and c["latency"]["undefined"] is True and c["latency"]["p50"] is None
    assert "0 ms" not in c["latency"]["text"] and c["prometheus_available"] is True


def test_few_requests_are_marked_as_too_few_to_mean_much(rig):
    c = day(rig, per_step={"200": 0}).build()
    assert c["requests"]["total"]["value"] == 0
    c = populate(rig.source, S - 3600, E + 600, per_step={"200": 1}) and rig.build(plan(start=utc(2026, 10, 10, 9, 59), end=END))
    assert c["latency"]["few"] is True and "too few to mean much" in c["latency"]["text"]


# Gaps and restarts ----------------------------------------------------------------------------------------------------------------

def test_missing_samples_inside_the_period_lose_nothing_and_are_listed(rig):
    mid = (S + E) / 2
    c = day(rig, outages=[(mid - 1500, mid + 1500)]).build()
    assert c["requests"]["total"]["value"] == 57_600 and c["requests"]["total"]["marks"] == ["incomplete"] and c["requests"]["total"]["text"] == "57,600"
    gap_note = [n for n in c["notes"] if "when monitoring has no samples" in n]
    assert len(gap_note) == 1 and "events happened somewhere between" in gap_note[0] and "They are counted in this report." in gap_note[0] and "start boundary" not in gap_note[0]
    assert c["coverage"] < 100


def test_a_restart_inside_the_period_is_at_least_and_explained(rig):
    mid = (S + E) / 2
    c = day(rig, restarts=[mid]).build()
    assert c["requests"]["total"]["text"].startswith("at least 57,") and "may_undercount" in c["requests"]["total"]["marks"]
    assert c["resources"]["restarts"]["text"] == "1" and any("restarted 1 time in this period" in n and "may undercount" in n for n in c["notes"])


def test_two_restarts_are_two(rig):
    c = day(rig, restarts=[S + 20_000, S + 50_000]).build()
    assert c["resources"]["restarts"]["text"] == "2" and any("2 times" in n for n in c["notes"])


def test_the_reports_either_side_of_an_outage_over_the_boundary_add_up_and_each_says_what_moved(rig):
    b0, b1, b2 = START, END, utc(2026, 10, 11, 10)
    outage = (E - 3 * 3600, E + 2 * 3600)                               # monitoring is down from 7pm to midnight Sydney, over the 9pm boundary
    populate(rig.source, b0.timestamp() - 3600, b2.timestamp() + 600, outages=[outage])
    a = rig.builder.build(plan(b0, b1), b1.timestamp() + 60)
    b = rig.builder.build(plan(b1, b2), b2.timestamp() + 60)
    whole = rig.builder.build(plan(b0, b2), b2.timestamp() + 60)
    assert a["requests"]["total"]["value"] + b["requests"]["total"]["value"] == whole["requests"]["total"]["value"]
    assert "incomplete" in a["requests"]["total"]["marks"] and "incomplete" in b["requests"]["total"]["marks"]
    assert any("the count stops at the sample at" in n and "Events after it are counted in the next report" in n for n in a["notes"])
    assert any("the sample used for the start boundary is" in n for n in b["notes"]) and any("some happened before the start boundary" in n for n in b["notes"])
    assert not any("when monitoring has no samples" in n for n in a["notes"])


def test_a_gap_with_a_restart_in_it_says_so(rig):
    mid = (S + E) / 2
    c = day(rig, outages=[(mid - 600, mid + 600)], restarts=[mid]).build()
    assert any("the API restarted inside that gap" in n for n in c["notes"])


def test_data_that_begins_after_the_start_is_said_to_start_late(rig):
    populate(rig.source, S + 7200, E + 600)
    c = rig.build()
    assert c["requests"]["total"]["marks"] == ["incomplete"] and any("no sample exists at or before the start boundary" in n for n in c["notes"])


# No data and no Prometheus ---------------------------------------------------------------------------------------------------------

def test_a_period_with_no_samples_says_no_data_and_shows_no_zeros(rig):
    c = rig.build()
    assert c["prometheus_available"] is True
    assert any(n.startswith("No data from ") and "so no figure below is a zero" in n for n in c["notes"])
    assert c["requests"]["total"] == {"value": None, "text": "no data", "marks": ["no_data"]}
    assert c["latency"]["text"] == "no data" and c["resources"]["restarts"]["text"] == "no data" and c["coverage"] is None
    assert all(f["text"] == "no data" for f in c["requests"]["by_status"].values())


def test_a_period_before_the_data_began_is_no_data_and_not_zero(rig):
    populate(rig.source, E + 1000, E + 5000)
    assert rig.build()["requests"]["total"]["text"] == "no data"


def test_when_prometheus_cannot_be_read_every_figure_is_no_data_and_the_rest_still_comes(rig, clock):
    day(rig)
    rig.source.down = True
    seed(rig.ledger, "a", "accepted", E, kind="alert", accepted_at=E - 100, size=10)
    c = rig.build()
    assert c["prometheus_available"] is False and any(n.startswith("Prometheus could not be read") for n in c["notes"])
    assert c["requests"]["total"]["text"] == "no data" and c["disk"]["free_percent"] == 60.0 and c["email"]["storage"] == "ok" and "bandwidth" in c


def test_too_many_samples_is_reported_as_such(rig):
    from cricket_notifier.prometheus import TooManySamples
    rig.source.matrix = lambda *a, **k: (_ for _ in ()).throw(TooManySamples("x"))
    c = rig.build()
    assert c["prometheus_available"] is False and any("too many samples" in n for n in c["notes"])


def test_the_first_report_says_its_period_starts_when_monitoring_first_ran(rig):
    day(rig)
    c = rig.build(plan(start=utc(2026, 10, 10, 6), kind="first_run"))
    assert c["period"]["start_kind"] == "first_run" and "first report" in c["period"]["first_run_note"] and "not at a scheduled boundary" in c["period"]["first_run_note"]


# The ledger, bandwidth and disk -------------------------------------------------------------------------------------------------

def test_the_email_section_counts_the_period_and_the_unreviewed_and_the_budget_used(rig):
    day(rig)
    L = rig.ledger
    for i in range(3):
        seed(L, f"a{i}", "accepted", E - 1000, kind="alert", size=10, accepted_at=E - 1000, charge_day="2026-10-10", charge_month="2026-10", context=json.dumps({"alertname": "ApiDown"}))
    seed(L, "r", "accepted", E - 900, kind="recovery", size=10, accepted_at=E - 900, charge_day="2026-10-10", charge_month="2026-10", context=json.dumps({"alertname": "ApiDown"}))
    seed(L, "f", "failed", E - 800, kind="alert", size=10)
    seed(L, "u", "unconfirmed", E - 700, kind="alert", size=10)
    seed(L, "b", "budget_suppressed", E - 600, kind="alert", size=0)
    seed(L, "s", "accepted", E - 500, kind="summary", size=10, accepted_at=E - 500, charge_day="2026-10-10", charge_month="2026-10")
    c = rig.build()
    e = c["email"]
    assert e["accepted"] == 5 and e["failed"] == 1 and e["unconfirmed"] == 1 and e["budget_suppressed"] == 1 and e["unreviewed_failed"] == 1 and e["unreviewed_unconfirmed"] == 1
    assert e["accepted_note"] == "accepted by Resend, delivery not tracked" and e["day_used"] == {"used": 5, "cap": 40} and e["month_used"] == {"used": 5, "cap": 900}
    assert c["alerts"] == {"fired": 6, "recovered": 1, "fired_names": [("ApiDown", 3), ("unnamed", 3)], "recovered_names": [("ApiDown", 1)]}


def test_alert_names_are_cut_and_cleaned(rig):
    day(rig)
    hostile = "Api<script>\nDown\x00" + "x" * 200
    seed(rig.ledger, "h", "accepted", E - 100, kind="alert", size=10, context=json.dumps({"alertname": hostile}))
    name = rig.build()["alerts"]["fired_names"][0][0]
    assert len(name) <= 60 and not any(c in name for c in "<>\n\x00")


def test_the_account_usage_resend_reported_is_shown_apart_and_only_for_its_own_day(rig):
    day(rig)
    rig.ledger.set_meta("observed", {"day": "2026-10-10", "daily_used": 61, "month": "2026-10", "monthly_used": 1500, "at": E})
    e = rig.build()["email"]
    assert e["account_day"] == 61 and e["account_month"] == 1500
    rig.ledger.set_meta("observed", {"day": "2026-10-09", "daily_used": 61, "month": "2026-09", "monthly_used": 1500, "at": E})
    e = rig.build()["email"]
    assert e["account_day"] is None and e["account_month"] is None


def test_skipped_slots_and_the_breaker_are_reported(rig):
    day(rig)
    seed(rig.ledger, "s-old", "skipped", E - 100, kind="summary", size=0, slot="summary/2026-10-07T10:00:00Z", due_at=E - 3 * 86400)
    rig.ledger.set_meta("breaker", {"state": "open", "failures": 3, "opened_at": E, "cooldown": 1800})
    e = rig.build()["email"]
    assert e["skipped_slots"] == ["Wed 7 Oct 2026, 21:00 +11:00 (Australia/Sydney)"] and e["breaker"] == "open"


def test_bandwidth_is_summed_by_whole_hours_with_unmeasured_hours_counted_apart(rig):
    day(rig)
    first, last = int(S) // 3600, int(E) // 3600
    month_first = int(utc(2026, 10, 1).timestamp()) // 3600
    with rig.ledger.txn() as conn:
        for h in range(month_first, last):
            if h in (first + 3, first + 4):
                continue                                                  # two hours with no row at all
            conn.execute("INSERT INTO bandwidth(hour, bytes, measured) VALUES(?,?,?)", (h, 1_000_000, 1))
        conn.execute("INSERT OR REPLACE INTO bandwidth(hour, bytes, measured) VALUES(?,?,?)", (first + 5, 0, 0))
    b = rig.build()["bandwidth"]
    assert b["period"]["hours"] == 24 and b["period"]["unmeasured_hours"] == 3 and b["period"]["bytes"] == 21_000_000
    assert b["period"]["text"] == "at least 20.03 MiB, with 3 of 24 hours unmeasured"
    assert b["month"]["unmeasured_hours"] == 3 and b["month"]["bytes"] == 1_000_000 * (last - month_first - 3) and b["allowance"] == "500 GB"
    assert "whole UTC hours" in b["note"]


def test_bandwidth_with_nothing_stored_is_no_data(rig):
    day(rig)
    b = rig.build()["bandwidth"]
    assert b["period"]["text"] == "no data" and b["month"]["text"] == "no data" and b["period"]["unmeasured_hours"] == b["period"]["hours"] == 24


def test_disk_is_read_directly_and_a_failure_is_no_data(rig, make_ledger):
    day(rig)
    assert rig.build()["disk"] == {"free_percent": 60.0, "free": "55.88 GiB", "total": "93.13 GiB"}
    rig.builder.free_space = lambda path: (_ for _ in ()).throw(OSError("nope"))
    assert rig.build()["disk"] == {"free_percent": None, "free": "no data", "total": "no data"}


# Whatever the situation ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [{}, {"traffic": False}, {"restarts": [S + 30_000]}, {"outages": [(S + 40_000, S + 50_000)]}])
def test_the_context_is_plain_data_and_the_same_every_time(rig, kwargs):
    day(rig, **kwargs)
    first = rig.build()
    assert json.loads(json.dumps(first)) is not None and rig.build() == first


def test_no_figure_is_ever_a_nan_or_an_infinity(rig):
    for kwargs in ({}, {"traffic": False}):
        day(rig, **kwargs)
        text = json.dumps(rig.build())
        assert "NaN" not in text and "Infinity" not in text


def test_a_day_of_each_kind_has_the_same_sections(rig):
    keys = None
    for kwargs in ({}, {"traffic": False}):
        day(rig, **kwargs)
        k = set(rig.build())
        keys = keys or k
        assert k == keys
    rig.source.series = {}
    assert set(rig.build()) == keys


def test_the_alert_names_in_a_summary_are_the_ones_the_webhook_really_stores(rig):
    """The names are read from what WebhookServer stores for each alert (messages.alert_stored_context), not from a context made by hand for the test: a mismatch of keys once showed every alert as 'unnamed'."""
    from cricket_notifier import messages
    from zoneinfo import ZoneInfo
    webhook = {"version": "4", "groupKey": "g", "status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "DiskUsedHigh", "severity": "high"}, "annotations": {"summary": "s"},
                                                                          "startsAt": "2026-10-09T20:00:00Z", "endsAt": "0001-01-01T00:00:00Z", "fingerprint": "f1"}]}
    stored = messages.alert_stored_context(messages.alert_context(webhook, ZoneInfo("Australia/Sydney")))
    rig.ledger.persist(kind="alert", key="alert/x", body=b'{"subject":"s","text":"t","html":"h","to":["a@b.org"],"from":"a@b.org"}', now=S + 3600, context=stored, deadline=S + 7200)
    resolved = dict(webhook, status="resolved")
    resolved["alerts"] = [dict(webhook["alerts"][0], status="resolved", endsAt="2026-10-09T21:00:00Z")]
    rig.ledger.persist(kind="recovery", key="recovery/x", body=b'{"subject":"s","text":"t","html":"h","to":["a@b.org"],"from":"a@b.org"}', now=S + 7200,
                       context=messages.alert_stored_context(messages.alert_context(resolved, ZoneInfo("Australia/Sydney"))), deadline=S + 9000)
    alerts = day(rig).build()["alerts"]
    assert alerts["fired_names"] == [("DiskUsedHigh", 1)] or alerts["fired_names"] == [["DiskUsedHigh", 1]]
    assert alerts["recovered_names"] in ([("DiskUsedHigh", 1)], [["DiskUsedHigh", 1]]) and "unnamed" not in json.dumps(alerts)


# The daily refresh in the report ---------------------------------------------------------------------------------------------

HOUR = 3600


def refreshed(rig, **refresh):
    return day(rig, refresh=refresh).build()["refresh"]


def test_no_refresh_series_says_it_is_not_installed_and_is_not_a_problem(rig):
    c = day(rig).build()
    assert c["refresh"]["state"] == "off" and "not installed" in c["refresh"]["headline"] and c["refresh"]["checked"] is None
    assert not any("refresh" in reason for reason in c["attention"])


def test_a_switched_off_refresh_says_so(rig):
    r = refreshed(rig, enabled=0)
    assert r["state"] == "off" and "switched off" in r["headline"]


def test_a_check_that_found_nothing_new_says_nothing_changed(rig):
    r = refreshed(rig, check=E - 6 * HOUR, activation=S - 10 * 86400)
    assert r["state"] == "ok" and r["headline"] == "Checked Cricsheet. Nothing new, so nothing changed."
    assert r["checked"].startswith("Last successful check of Cricsheet: ") and "No new data in this period." in r["new_data"] and "published" in r["new_data"]


def test_new_data_published_in_the_period_says_when_and_which_version_replaced_which(rig):
    mid = (S + E) / 2
    c = day(rig, refresh={"check": mid, "activation": mid}, versions=[(0, "aaaaaaaaaaaa"), (mid, "bbbbbbbbbbbb")]).build()
    r = c["refresh"]
    assert r["state"] == "ok" and r["headline"] == "Checked Cricsheet and published new data."
    assert r["new_data"].startswith("New data was published at ") and "Version aaaaaaaaaaaa was replaced by bbbbbbbbbbbb." in r["new_data"]
    assert not any("refresh" in a.lower() for a in c["attention"])


def test_a_check_that_found_nothing_does_not_claim_a_new_version(rig):
    r = refreshed(rig, check=E - HOUR, activation=S - 86400)
    assert "replaced" not in r["new_data"] and "New data was published" not in r["new_data"]


def test_a_failed_refresh_needs_attention_and_says_the_live_data_was_not_changed(rig):
    c = day(rig, refresh={"check": E - HOUR, "failed": 1}).build()
    assert c["refresh"]["state"] == "attention" and "The data being served was not changed" in c["refresh"]["headline"]
    assert c["refresh"]["headline"] in c["attention"]


def test_unfinished_cache_clearing_needs_attention(rig):
    r = refreshed(rig, check=E - HOUR, activation=E - HOUR, pending=1)
    assert r["state"] == "attention" and "clearing the cache" in r["headline"]


def test_a_refresh_nobody_has_observed_for_half_an_hour_needs_attention(rig):
    r = refreshed(rig, check=E - HOUR, observed=E - 2 * HOUR)
    assert r["state"] == "attention" and "not being observed" in r["headline"]


def test_no_successful_check_for_more_than_36_hours_needs_attention(rig):
    r = refreshed(rig, check=E - 40 * HOUR)
    assert r["state"] == "attention" and "36 hours" in r["headline"]
    assert refreshed(rig, check=0)["state"] == "attention", "no successful check ever recorded is not a healthy refresh"


def test_a_failure_that_cleared_is_reported_as_worked_after_a_failure(rig):
    mid = (S + E) / 2
    r = refreshed(rig, check=E - HOUR, failed=lambda t: 1 if t < mid else 0)
    assert r["state"] == "ok" and "after a failure" in r["headline"]


def test_the_routine_boundary_note_stays_in_the_data_but_is_not_shown(rig):
    c = day(rig).build()
    assert c["notes"] and c["shown_notes"] == []


def test_attention_lists_only_what_is_wrong_in_plain_words(rig):
    c = day(rig, per_step={"200": 10}).build()
    assert c["attention"] == [] and c["incomplete"] is False
    c = day(rig).build()
    assert c["attention"] == ["5,760 server errors (5xx)."]


def test_a_report_that_could_not_be_built_still_carries_the_new_keys(rig):
    c = rig.builder.fallback_context(plan(), E + 60, "test")
    assert c["refresh"]["state"] == "unknown" and c["attention"] and c["shown_notes"] == c["notes"]


# An earlier publication followed by a newer attempt ---------------------------------------------------------------------------

def test_a_publication_earlier_in_the_period_and_a_newer_still_running_check_do_not_read_as_one_publication(rig):
    """The 2026-10-29 report: data published the evening before, then a later attempt's check, with nothing yet published by that attempt. The headline and rows must not join the
    newer check to the older publication."""
    published, later_check = S + 2 * HOUR, E - HOUR
    c = day(rig, refresh={"check": later_check, "activation": published}, versions=[(0, "aaaaaaaaaaaa"), (published, "bbbbbbbbbbbb")]).build()
    r = c["refresh"]
    assert r["state"] == "ok" and r["headline"] == "New data was published earlier in this period. A newer check had not published anything yet."
    assert "Checked Cricsheet and published" not in r["headline"]
    assert r["new_data"].startswith("New data was published earlier in this period, at ") and "Version aaaaaaaaaaaa was replaced by bbbbbbbbbbbb." in r["new_data"]
    assert r["checked"].startswith("Latest check of Cricsheet: ") and "came after that publication" in r["checked"] and "had not published anything when this report was made" in r["checked"]
    assert not any("refresh" in a.lower() for a in c["attention"])


def test_a_check_in_the_same_attempt_as_the_publication_still_reads_as_checked_and_published(rig):
    mid = (S + E) / 2
    r = day(rig, refresh={"check": mid, "activation": mid}, versions=[(0, "aaaaaaaaaaaa"), (mid, "bbbbbbbbbbbb")]).build()["refresh"]
    assert r["headline"] == "Checked Cricsheet and published new data." and r["new_data"].startswith("New data was published at ") and "earlier" not in r["new_data"]
    assert r["checked"].startswith("Last successful check of Cricsheet: ")


def test_a_failed_refresh_after_an_earlier_publication_still_needs_attention(rig):
    published = S + 2 * HOUR
    r = day(rig, refresh={"check": E - HOUR, "activation": published, "failed": 1}, versions=[(0, "aaaaaaaaaaaa"), (published, "bbbbbbbbbbbb")]).build()["refresh"]
    assert r["state"] == "attention" and "failed" in r["headline"]


def test_times_in_the_refresh_rows_are_short_and_the_zone_is_said_once(rig):
    r = refreshed(rig, check=E - 6 * HOUR, activation=S - 3 * 86400)
    assert "Australia/Sydney)" not in r["checked"] and "+11:00" not in r["checked"] and "+11:00" not in r["new_data"]
    assert r["checked"] == "Last successful check of Cricsheet: Sat 10 Oct, 15:00." and r["times_note"] == "Times in this section are Australia/Sydney time (+11:00)."
    assert r["new_data"] == "No new data in this period. The data now live was published Tue 6 Oct, 21:00."


def test_a_time_across_a_clock_change_keeps_its_own_offset(rig):
    r = refreshed(rig, check=E - 6 * HOUR, activation=utc(2026, 10, 3, 10).timestamp())      # Sydney moved to summer time on 4 October
    assert "Sat 3 Oct 2026, 20:00 +10:00 (Australia/Sydney)" in r["new_data"]


def write_result(folder, **fields):
    value = {"schema_version": 1, "key": "a" * 32, "final": True, "outcome": "published", "started_utc": "2026-10-09T05:00:00Z", "ended_utc": "2026-10-09T08:00:00Z", "recorded_utc": "2026-10-09T08:00:00Z",
             "phases": [], "previous_version": "aaaaaaaaaaaa", "served_version": "bbbbbbbbbbbb", "previous_display_name": "Dataset v3", "served_display_name": "Data of 2026-10-08",
             "latest_match_date": "2026-10-07", "reason": None, "journal_phase": "committed", **fields}
    (folder / (value["key"] + ".json")).write_text(json.dumps(value))


def test_the_friendly_dataset_name_comes_first_and_the_hash_second(make_ledger, tmp_path):
    folder = tmp_path / "results"
    folder.mkdir()
    write_result(folder)
    ledger = make_ledger(refresh={"results_dir": str(folder)})
    builder = SummaryBuilder(FakeSource(), ledger, ledger.config, free_space=lambda path: Disk(100 * 10 ** 9, 40 * 10 ** 9, 60 * 10 ** 9))
    mid = (S + E) / 2
    populate(builder.source, S - 3600, E + 600, built=S - 7200, data_version="bbbbbbbbbbbb", refresh={"check": mid, "activation": mid}, versions=[(0, "aaaaaaaaaaaa"), (mid, "bbbbbbbbbbbb")])
    c = builder.build(plan(), E + 60)
    assert c["data"]["name"] == "Data of 2026-10-08" and c["data"]["label"] == "Data of 2026-10-08 (bbbbbbbbbbbb)"
    assert "Dataset v3 (aaaaaaaaaaaa) was replaced by Data of 2026-10-08 (bbbbbbbbbbbb)." in c["refresh"]["new_data"]


def test_a_version_with_no_recorded_name_is_shown_by_its_hash_alone(rig):
    c = day(rig, built=S - 7200).build()
    assert c["data"]["name"] is None and c["data"]["label"] == "version v1"
