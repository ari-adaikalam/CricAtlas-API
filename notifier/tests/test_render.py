"""Rendering: every kind, escaping, a missing variable, templates kept apart from the code, the sandbox, and the size limit with its fallbacks."""
import json
import re
import shutil
from pathlib import Path

import jinja2
import pytest

from cricket_notifier import messages, payload
from cricket_notifier.errors import PayloadTooLarge
from cricket_notifier.render import FOOTER, KINDS, Renderer, mark_words, shrink
from cricket_notifier.schedule import describe
from zoneinfo import ZoneInfo

from fakes import FakeSource, populate
from test_summary import S, E, START, END, plan, utc, Disk
from cricket_notifier.summary import SummaryBuilder

ZONE = ZoneInfo("Australia/Sydney")
TEMPLATES = Path(__file__).parent.parent / "cricket_notifier" / "templates"


@pytest.fixture
def renderer(config):
    return Renderer(config)


@pytest.fixture
def summary_context(make_ledger):
    ledger = make_ledger()

    def build(**kw):
        source = FakeSource()
        if kw.pop("empty", False):
            pass
        else:
            populate(source, S - 3600, E + 600, **kw)
        return SummaryBuilder(source, ledger, ledger.config, free_space=lambda p: Disk(100 * 10 ** 9, 40 * 10 ** 9, 60 * 10 ** 9)).build(plan(), E + 60)
    return build


def webhook(n=1, status="firing", **fields):
    alerts = [{"labels": {"alertname": f"Alert{i}", "severity": "page", "instance": "10.0.0.5:9100"}, "annotations": {"summary": f"summary {i}", "description": "d" * 50}, "fingerprint": f"fp{i}",
               "startsAt": "2026-10-10T08:00:00.123Z", "endsAt": "2026-10-10T09:30:00Z" if status == "resolved" else "0001-01-01T00:00:00Z"} for i in range(n)]
    return {"version": "4", "groupKey": "g", "status": status, "alerts": alerts, "commonLabels": {"alertname": "Alert0"}, **fields}


CONTEXTS = {
    "alert": lambda: messages.alert_context(webhook(3), ZONE),
    "recovery": lambda: messages.alert_context(webhook(2, "resolved"), ZONE),
    "digest": lambda: messages.digest_context([{"kind": "alert", "created_at": 1, "context": {"group_name": "ApiDown"}}, {"kind": "recovery", "created_at": 2, "context": {"group_name": "ApiDown"}}],
                                              ["DiskLow"], "Sat 10 Oct 2026, 08:00 +11:00", "Sat 10 Oct 2026, 09:00 +11:00", ZONE),
    "suppression": lambda: {"reason": "budget", "count": 12, "day": "2026-10-10"},
    "email_path": lambda: {"state": "failing", "conditions": ["a summary is overdue", "2 messages are past their deadline"], "failed": 2, "unconfirmed": 1, "breaker": "open", "since": "Sat 10 Oct 2026, 08:00 +11:00",
                           "duration": None},
    "host": lambda: {"state": "low", "free_percent": 9.5, "free": "3.80 GiB", "total": "40.00 GiB", "path": "/"},
}


def test_the_template_folder_has_every_file_for_every_kind():
    expected = {"base.txt", "base.html"} | {f"{k}.{ext}" for k in KINDS for ext in ("subject.txt", "txt", "html")}
    assert {p.name for p in TEMPLATES.iterdir()} == expected


@pytest.mark.parametrize("kind", sorted(CONTEXTS))
def test_each_kind_renders_a_subject_a_text_and_html(renderer, kind):
    r = renderer.render(kind, CONTEXTS[kind]())
    assert r.subject.startswith("[CricAtlas API] ") and "\n" not in r.subject and 20 < len(r.subject) <= 150
    assert len(r.text) > 100 and r.html.startswith("<!doctype html>") and "</html>" in r.html and r.text.rstrip().endswith(FOOTER) and FOOTER in r.html
    assert r.subject in r.text.splitlines()[0] and f"<h1" in r.html


def test_the_summary_renders_all_three_parts(renderer, summary_context):
    r = renderer.render("summary", summary_context())
    assert r.subject == "[CricAtlas API] Daily summary, Sat 10 Oct 2026 (needs attention)" and "Requests            57,600" in r.text and "57,600" in r.html and "incomplete" not in r.subject
    assert "NEEDS ATTENTION\n  - 5,760 server errors (5xx)." in r.text and "Needs attention" in r.html, "the busy fixture day has server errors, and the top of the message says so"
    assert "Fri 9 Oct 2026, 21:00 +11:00 (Australia/Sydney)\n  to Sat 10 Oct 2026, 21:00" in r.text and "2026-10-10 10:00:00 UTC" in r.text and "Length 24 h" in r.text


def test_a_day_with_no_traffic_says_zero_requests_and_undefined_latency(renderer, summary_context):
    r = renderer.render("summary", summary_context(traffic=False))
    assert "Requests            0\n" in r.text and "no requests, so no response time" in r.text and "no requests, so no response time" in r.html and "0 ms" not in r.text
    assert "no data" not in r.text.split("TRAFFIC (")[1].split("SERVER")[0]


def test_a_period_with_no_samples_shows_no_data_and_no_zeros(renderer, summary_context):
    r = renderer.render("summary", summary_context(empty=True))
    traffic = r.text.split("TRAFFIC (")[1].split("SERVER")[0]
    assert "Requests            no data [no data]" in traffic and "No data from " in r.text and "0\n" not in traffic.split("Requests")[1].split("\n")[0]
    assert r.subject.endswith("(incomplete data)")


def test_an_incomplete_period_is_flagged_in_the_subject_and_its_notes_are_listed(renderer, summary_context):
    mid = (S + E) / 2
    r = renderer.render("summary", summary_context(outages=[(mid - 1500, mid + 1500)]))
    assert r.subject.endswith("(incomplete data)") and "57,600 [incomplete, see the notes]" in r.text
    assert re.search(r"NOTES \(why a figure above may be incomplete\)\n  - Requests: ", r.text) and "events happened somewhere between" in r.text and "events happened somewhere between" in r.html
    assert "counted from the sample at" not in r.text and "counted from the sample at" not in r.html, "the routine boundary line is kept in the data but not printed"


def test_a_restart_shows_at_least(renderer, summary_context):
    r = renderer.render("summary", summary_context(restarts=[(S + E) / 2]))
    assert "at least 57," in r.text and "may undercount, see the notes" in r.text and "restarted 1 time" in r.text


def test_the_first_report_says_so(renderer, make_ledger):
    ledger = make_ledger()
    source = FakeSource()
    populate(source, S - 3600, E + 600)
    context = SummaryBuilder(source, ledger, ledger.config, free_space=lambda p: Disk(100, 40, 60)).build(plan(start=utc(2026, 10, 10, 6), kind="first_run"), E + 60)
    r = renderer.render("summary", context)
    assert "This is the first report" in r.text and "This is the first report" in r.html


def test_marks_are_words():
    assert mark_words([]) == "" and mark_words(["no_data"]) == " [no data]" and mark_words(["highest_observed"]) == ""
    assert mark_words(["incomplete", "may_undercount"]) == " [incomplete, see the notes; may undercount, see the notes]"


# Escaping, subject, sandbox -------------------------------------------------------------------------------------------------

HOSTILE = '<script>alert(1)</script> "><img src=x onerror=alert(2)> & \' </td>'


def test_html_is_escaped_and_text_is_not(renderer):
    context = messages.alert_context(webhook(1, **{}), ZONE)
    context["alerts"][0]["summary"] = HOSTILE
    context["alerts"][0]["description"] = HOSTILE
    r = renderer.render("alert", context)
    assert "<script>" not in r.html and "<img src=x" not in r.html and "&lt;script&gt;" in r.html and "&amp;" in r.html and "onerror=alert(2)" in r.html.replace("&#34;", '"')
    assert HOSTILE in r.text and "&lt;" not in r.text, "plain text is not HTML-escaped"


def test_the_subject_is_one_clean_line_whatever_the_context_holds(renderer):
    context = messages.alert_context(webhook(1), ZONE)
    context["group_name"] = "Api\r\nBcc: attacker@example.org\x00" + "x" * 500
    r = renderer.render("alert", context)
    assert "\n" not in r.subject and "\r" not in r.subject and "\x00" not in r.subject and len(r.subject) <= 150


def test_a_variable_a_template_needs_but_the_context_lacks_is_an_error_not_a_blank(renderer):
    with pytest.raises(jinja2.UndefinedError):
        renderer.render("suppression", {"reason": "budget", "count": 1})
    with pytest.raises(jinja2.UndefinedError):
        renderer.render("summary", {"period": {}})


def test_an_unknown_kind_is_refused(renderer):
    with pytest.raises(ValueError):
        renderer.render("nonsense", {})


def test_the_templates_run_in_the_sandbox(config, tmp_path):
    folder = tmp_path / "templates"
    shutil.copytree(TEMPLATES, folder)
    (folder / "host.txt").write_text("{{ ''.__class__.__mro__ }}")
    with pytest.raises(jinja2.exceptions.SecurityError):
        Renderer(config, folder).render("host", CONTEXTS["host"]())


# Templates are apart from the code ------------------------------------------------------------------------------------------

def test_a_redesign_is_a_change_to_template_files_only(config, tmp_path):
    folder = tmp_path / "redesign"
    shutil.copytree(TEMPLATES, folder)
    (folder / "host.txt").write_text("REDESIGNED {{ free_percent }}\n")
    (folder / "host.subject.txt").write_text("{{ prefix }} NEW SUBJECT\n")
    new, old = Renderer(config, folder).render("host", CONTEXTS["host"]()), Renderer(config).render("host", CONTEXTS["host"]())
    assert new.text == "REDESIGNED 9.5\n" and new.subject == "[CricAtlas API] NEW SUBJECT" and old.text != new.text
    assert new.html.replace("NEW SUBJECT", "Disk space low") == old.html, "the HTML file that was not changed differs only by the new subject it shows as its title"


def test_the_renderer_and_the_templates_know_nothing_of_secrets_or_scheduling():
    code = (Path(__file__).parent.parent / "cricket_notifier" / "render.py").read_text()
    imports = re.findall(r"^(?:from|import) (\S+)", code, re.M)
    assert not [m for m in imports if any(x in m for x in ("secrets_io", "ledger", "resend", "schedule", "sender", "budget"))], imports
    for word in ("secret", "token", "api_key"):
        assert word not in code.lower(), word
    for path in TEMPLATES.iterdir():
        assert not re.search(r"secret|token|password|api[_ ]?key", path.read_text(), re.I), path.name


def test_no_template_uses_a_dash_as_punctuation():
    for path in TEMPLATES.iterdir():
        text = path.read_text()
        assert "—" not in text and "–" not in text and " -- " not in text, path.name


def test_the_rendered_text_has_no_dash_punctuation_either(renderer, summary_context):
    outputs = [renderer.render(k, CONTEXTS[k]()) for k in CONTEXTS] + [renderer.render("summary", summary_context(outages=[(S + 40_000, S + 50_000)]))]
    for r in outputs:
        for part in (r.subject, r.text):
            assert "—" not in part and "–" not in part and " -- " not in part


# The bytes and the size limit ---------------------------------------------------------------------------------------------------

def test_the_payload_is_canonical_json_and_the_same_every_time(renderer, config):
    rendered, body = renderer.message("host", CONTEXTS["host"]())
    again = renderer.message("host", CONTEXTS["host"]())[1]
    assert body == again and json.loads(body) == {"from": config.email.sender, "to": [config.email.recipient], "subject": rendered.subject, "html": rendered.html, "text": rendered.text}
    assert list(json.loads(body)) == sorted(json.loads(body)) and b": " not in body.split(b'"html"')[0] and body == payload.build(config.email.sender, config.email.recipient, rendered.subject, rendered.html, rendered.text)


@pytest.mark.parametrize("kind, status, at_least", [("alert", "firing", 20_000), ("recovery", "resolved", 10_000)])
def test_the_largest_alert_or_recovery_the_builder_can_make_is_inside_its_limit_with_no_fallback(renderer, kind, status, at_least):
    big = webhook(100, status)
    for a in big["alerts"]:
        a["labels"] = {"alertname": "A" * 500, "severity": "S" * 500}
        a["annotations"] = {"summary": "&" * 5000, "description": "&" * 5000}                    # the character that grows most when escaped: five bytes for one
        a["labels"] = {"alertname": "&" * 500, "severity": "&" * 500}
    context = messages.alert_context(big, ZONE)
    assert len(context["alerts"]) == 10 and context["more"] == 90 and len(context["alerts"][0]["summary"]) == 120 and len(context["alerts"][0]["name"]) == 60
    rendered, body = renderer.message(kind, context)
    assert len(body) <= payload.OTHER_MAX_BYTES and renderer.counters == {"shrunk": 0, "minimal": 0}, len(body)
    assert len(body) > at_least, "the test really did build a large message, worst case: every character escaped to five bytes"


def test_the_largest_digest_and_notices_are_inside_their_limits(renderer):
    held = [{"kind": "alert" if i % 2 else "recovery", "created_at": i, "context": {"group_name": f"{i:03d}" + "n" * 200}} for i in range(500)]
    digest = messages.digest_context(held, [f"{i:03d}" + "x" * 200 for i in range(100)], "o" * 300, "c" * 300, ZONE)
    for kind, context in (("digest", digest), ("email_path", {"state": "failing", "conditions": ["c" * 200] * 6, "failed": 10 ** 6, "unconfirmed": 10 ** 6, "breaker": "open", "since": "s" * 100, "duration": None}),
                          ("suppression", {"reason": "budget", "count": 10 ** 9, "day": "2026-10-10"}), ("host", {"state": "low", "free_percent": 0.0, "free": "x" * 30, "total": "y" * 30, "path": "/" + "p" * 200})):
        rendered, body = renderer.message(kind, context)
        assert len(body) <= payload.OTHER_MAX_BYTES, kind
    assert renderer.counters == {"shrunk": 0, "minimal": 0}


def hostile_summary(summary_context, notes=60, size=2000):
    context = summary_context()
    context["notes"] = context["shown_notes"] = ["<" * size] * notes
    return context


def test_a_summary_over_its_limit_is_shrunk_and_still_inside_it(renderer, summary_context):
    context = hostile_summary(summary_context)
    full = renderer.render("summary", context)
    assert len(payload.build("a@b.co", "c@d.co", full.subject, full.html, full.text)) > payload.SUMMARY_MAX_BYTES
    rendered, body = renderer.message("summary", context)
    assert len(body) <= payload.SUMMARY_MAX_BYTES and renderer.counters == {"shrunk": 1, "minimal": 0}
    assert "Requests            57,600" in rendered.text and rendered.text.split("NOTES")[1].count("\n  - ") == 3 and "Some detail was left out because the message was too long." in rendered.text
    assert "Some detail was left out" in rendered.html and rendered.html.split("Notes (why")[1].count("<li>") == 3


def test_a_message_that_is_still_too_large_is_replaced_by_the_fixed_minimal_one(renderer, summary_context, monkeypatch):
    monkeypatch.setattr(payload, "SUMMARY_MAX_BYTES", 6000)
    rendered, body = renderer.message("summary", summary_context())
    assert len(body) <= 6000 and renderer.counters == {"shrunk": 0, "minimal": 1}
    assert "The full message was too large to send" in rendered.text and "Kind: summary" in rendered.text and rendered.subject == "[CricAtlas API] summary message"


def test_even_the_minimal_message_over_the_limit_is_refused(renderer, monkeypatch):
    monkeypatch.setattr(payload, "OTHER_MAX_BYTES", 500)
    with pytest.raises(PayloadTooLarge):
        renderer.message("host", CONTEXTS["host"]())


def test_shrinking_keeps_the_keys_and_cuts_lists_and_strings():
    context = {"a": list(range(10)), "b": {"c": "x" * 500, "d": [{"e": "y" * 500}] * 9}, "n": 5}
    out = shrink(context)
    assert set(out) == set(context) and len(out["a"]) == 3 and len(out["b"]["c"]) == 80 and len(out["b"]["d"]) == 3 and len(out["b"]["d"][0]["e"]) == 80 and out["n"] == 5
    assert len(context["a"]) == 10, "the original is not changed"


def test_many_gaps_in_a_period_are_listed_up_to_ten_and_then_counted(make_ledger, renderer):
    ledger = make_ledger()
    source = FakeSource()
    outages = [(S + 1000 + i * 600, S + 1000 + i * 600 + 100) for i in range(60)]
    populate(source, S - 3600, E + 600, outages=outages)
    context = SummaryBuilder(source, ledger, ledger.config, free_space=lambda p: Disk(100, 40, 60)).build(plan(), E + 60)
    gap_notes = [n for n in context["notes"] if "when monitoring has no samples" in n]
    assert len(gap_notes) == 10 and any("and 50 more gaps" in n for n in context["notes"])
    rendered, body = renderer.message("summary", context)
    assert len(body) <= payload.SUMMARY_MAX_BYTES and renderer.counters == {"shrunk": 0, "minimal": 0}


# Branding and the daily refresh in the message ------------------------------------------------------------------------------

LOGO = '<img src="https://cricatlas.ariadaikalam.com/brand/icons/icon-A-leather-88.png" width="28" height="28" alt="" style="border:0;width:28px;height:28px;vertical-align:middle;margin-right:2px;">'


def test_the_only_thing_a_message_fetches_is_the_logo_on_our_own_site(renderer):
    """Whatever the look of the templates, a message holds no script, link tag, web font or remote address, and its one image is the CricAtlas ball."""
    for kind in sorted(CONTEXTS):
        html = renderer.render(kind, CONTEXTS[kind]()).html
        assert re.findall(r"<img [^>]*>", html) == [LOGO] and "<script" not in html and "<link" not in html and "@import" not in html and "url(" not in html, kind
        assert re.findall(r'(?:src|href)="https?://[^"]*', html) == ['href="https://cricatlas.ariadaikalam.com/', 'src="https://cricatlas.ariadaikalam.com/brand/icons/icon-A-leather-88.png'], f"{kind} must not call out except for the logo and its link home"


def test_the_footer_carries_no_delivery_notice():
    from cricket_notifier.render import FOOTER
    assert "Resend" not in FOOTER and "Delivery" not in FOOTER


def test_the_summary_shows_the_daily_refresh_outcome_first_and_in_plain_words(renderer, make_ledger):
    ledger = make_ledger()
    source = FakeSource()
    mid = (S + E) / 2
    populate(source, S - 3600, E + 600, per_step={"200": 10}, refresh={"check": mid, "activation": mid}, versions=[(0, "aaaaaaaaaaaa"), (mid, "bbbbbbbbbbbb")])
    context = SummaryBuilder(source, ledger, ledger.config, free_space=lambda p: Disk(100, 40, 60)).build(plan(), E + 60)
    r = renderer.render("summary", context)
    assert r.subject == "[CricAtlas API] Daily summary, Sat 10 Oct 2026" and "ALL NORMAL. Nothing needs attention." in r.text
    block = r.text.split("DAILY REFRESH")[1].split("TRAFFIC (")[0]
    assert "Checked Cricsheet and published new data." in block and "Version aaaaaaaaaaaa was replaced by bbbbbbbbbbbb." in block and "Data now live" in block
    assert "Checked Cricsheet and published new data." in r.html and "All normal." in r.html and "Daily refresh" in r.html


def test_a_failed_refresh_is_at_the_top_of_the_message_and_in_the_subject(renderer, make_ledger):
    ledger = make_ledger()
    source = FakeSource()
    populate(source, S - 3600, E + 600, per_step={"200": 10}, refresh={"check": E - 3600, "failed": 1})
    context = SummaryBuilder(source, ledger, ledger.config, free_space=lambda p: Disk(100, 40, 60)).build(plan(), E + 60)
    r = renderer.render("summary", context)
    assert r.subject.endswith("(needs attention)") and r.text.index("NEEDS ATTENTION") < r.text.index("PERIOD") < r.text.index("DAILY REFRESH")
    assert "The refresh failed. The data being served was not changed." in r.text.split("PERIOD")[0] and "Needs attention" in r.html


def test_the_message_has_no_jargon_the_owner_could_not_read_without_a_manual(renderer, summary_context):
    r = renderer.render("summary", summary_context(traffic=False))
    for jargon in ("p50", "interpolation", "bucket width", "Samples present", "Circuit breaker", "Ledger storage", "highest observed", "Budget used (UTC)", "Faults  "):
        assert jargon not in r.text, jargon


def test_the_summary_shows_the_dataset_name_first_and_short_times(renderer, make_ledger, tmp_path):
    import json as _json
    folder = tmp_path / "results"
    folder.mkdir()
    (folder / ("a" * 32 + ".json")).write_text(_json.dumps({"schema_version": 1, "key": "a" * 32, "final": True, "outcome": "published", "started_utc": "2026-10-09T05:00:00Z", "ended_utc": "2026-10-09T08:00:00Z",
        "recorded_utc": "2026-10-09T08:00:00Z", "phases": [], "previous_version": "aaaaaaaaaaaa", "served_version": "bbbbbbbbbbbb", "previous_display_name": "Dataset v3", "served_display_name": "Data of 2026-10-08",
        "latest_match_date": "2026-10-07", "reason": None, "journal_phase": "committed"}))
    ledger = make_ledger(refresh={"results_dir": str(folder)})
    source = FakeSource()
    published = S + 7200
    populate(source, S - 3600, E + 600, built=S - 7200, data_version="bbbbbbbbbbbb", refresh={"check": E - 3600, "activation": published}, versions=[(0, "aaaaaaaaaaaa"), (published, "bbbbbbbbbbbb")])
    context = SummaryBuilder(source, ledger, ledger.config, free_space=lambda p: Disk(100 * 10 ** 9, 40 * 10 ** 9, 60 * 10 ** 9)).build(plan(), E + 60)
    r = renderer.render("summary", context)
    for body in (r.text, r.html):
        assert "Data of 2026-10-08 (bbbbbbbbbbbb), built Fri 9 Oct, 19:00" in body
        assert "New data was published earlier in this period" in body and "Checked Cricsheet and published" not in body
        assert "Times in this section are Australia/Sydney time (+11:00)." in body
    assert "Latest check of Cricsheet: Sat 10 Oct, 20:00." in r.text
