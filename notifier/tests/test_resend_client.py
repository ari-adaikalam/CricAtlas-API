"""The Resend client against a real local server: what each answer becomes, and that nothing it was told is kept."""
import logging

import pytest

from cricket_notifier import resend
from cricket_notifier.resend import ResendClient

from conftest import KEY, body_for


def client_for(standin, timeout=0.6):
    return ResendClient(standin.url, KEY, timeout=timeout)


def test_an_accepted_send_has_an_id_and_the_quota_figures(standin):
    standin.daily_quota, standin.monthly_quota = 4, 40
    outcome = client_for(standin).send(body_for(), "k1")
    assert outcome.kind == "accepted" and outcome.resend_id == "em_000001" and outcome.status == 200
    assert (outcome.daily_used, outcome.monthly_used) == (5, 41)
    assert standin.requests[0]["auth"] == f"Bearer {KEY}" and standin.requests[0]["key"] == "k1"


def test_the_same_key_and_bytes_return_the_original_and_send_nothing_new(standin):
    c = client_for(standin)
    first, second = c.send(body_for(), "k1"), c.send(body_for(), "k1")
    assert first.resend_id == second.resend_id and standin.sent == 1


def test_the_same_key_with_other_bytes_is_a_changed_payload(standin):
    c = client_for(standin)
    c.send(body_for("a"), "k1")
    outcome = c.send(body_for("b"), "k1")
    assert outcome.kind == "changed_payload" and outcome.error_name == "invalid_idempotent_request" and standin.sent == 1


@pytest.mark.parametrize("behaviour, kind, definitive, name", [
    ("429", "rate_limited", True, "rate_limit_exceeded"), ("429_daily", "quota_daily", True, "daily_quota_exceeded"), ("429_monthly", "quota_monthly", True, "monthly_quota_exceeded"),
    ("409_concurrent", "concurrent", False, "concurrent_idempotent_requests"), ("409_changed", "changed_payload", True, "invalid_idempotent_request"),
    ("400", "permanent", True, "invalid_parameter"), ("401", "permanent", True, "missing_api_key"), ("403", "permanent", True, "invalid_from_address"), ("422", "permanent", True, "validation_error"),
    ("500", "server_error", False, "internal_server_error"), ("503", "server_error", False, "application_error"),
])
def test_each_answer_becomes_one_outcome(standin, behaviour, kind, definitive, name):
    standin.script = [behaviour]
    outcome = client_for(standin).send(body_for(), "k")
    assert outcome.kind == kind and outcome.definitive is definitive and outcome.error_name == name and standin.sent == 0


def test_a_rate_limit_carries_its_retry_after(standin):
    standin.script = [{"kind": "429", "retry_after": 7}]
    assert client_for(standin).send(body_for(), "k").retry_after == 7


def test_a_timeout_and_a_reset_connection_are_unknown_outcomes(standin):
    standin.script = ["timeout", "drop"]
    c = client_for(standin, timeout=0.3)
    for _ in range(2):
        outcome = c.send(body_for(), "k")
        assert outcome.kind == "network_error" and outcome.definitive is False


def test_a_timeout_after_the_email_was_accepted_looks_the_same_as_one_before_it(standin):
    """The case that matters: the notifier cannot tell these apart, and only a repeat with the same key settles it."""
    standin.script = ["accept_then_timeout"]
    c = client_for(standin, timeout=0.3)
    outcome = c.send(body_for(), "k")
    assert outcome.kind == "network_error" and standin.sent == 1
    standin.script = []
    again = c.send(body_for(), "k")
    assert again.kind == "accepted" and again.resend_id == "em_000001" and standin.sent == 1


def test_a_connection_that_closes_after_acceptance_is_unknown_and_settled_by_a_repeat(standin):
    standin.script = ["accept_then_reset"]
    c = client_for(standin)
    assert c.send(body_for(), "k").kind == "network_error" and standin.sent == 1
    assert c.send(body_for(), "k").resend_id == "em_000001" and standin.sent == 1


def test_a_redirect_is_not_followed_so_the_key_cannot_go_elsewhere(standin):
    standin.script = ["redirect"]
    outcome = client_for(standin).send(body_for(), "k")
    assert outcome.kind == "permanent" and outcome.error_name == "redirect" and len(standin.requests) == 1


def test_a_success_without_an_id_is_not_recorded_as_accepted(standin):
    standin.script = ["no_id"]
    outcome = client_for(standin).send(body_for(), "k")
    assert outcome.kind == "server_error" and outcome.error_name == "no_message_id"


def test_a_non_json_error_page_is_a_server_error(standin):
    standin.script = ["html"]
    assert client_for(standin).send(body_for(), "k").kind == "server_error"


def test_a_server_that_is_not_there_is_a_network_error():
    assert ResendClient("http://127.0.0.1:1", KEY, timeout=0.5).send(body_for(), "k").kind == "network_error"


def test_the_text_of_a_response_is_never_kept(standin):
    standin.script = ["500"]
    outcome = client_for(standin).send(body_for(), "k")
    assert "STANDINTEXT" not in repr(outcome) and "STANDINTEXT" not in str(outcome.__dict__)


def test_the_key_is_not_in_the_client_repr_or_in_any_log(standin, caplog):
    c = client_for(standin)
    with caplog.at_level(logging.DEBUG):
        standin.script = ["500", "timeout"]
        c.send(body_for(), "k")
        c.send(body_for(), "k")
    assert KEY not in repr(c) and KEY not in caplog.text


@pytest.mark.parametrize("name", ["Daily_Quota", "x" * 100, "a b", "../x", "", None, 5])
def test_an_error_name_that_is_not_a_plain_name_is_dropped(name):
    class Headers(dict):
        pass
    import json
    outcome = ResendClient._classify(400, Headers(), json.dumps({"name": name}).encode())
    assert outcome.kind == "permanent" and outcome.error_name == "other"


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"5", b"null", b'{"id": 5}', b'{"id": ""}', b'{"id": "has space"}', b'{"id": "' + b"a" * 200 + b'"}'])
def test_a_success_with_a_bad_body_is_never_accepted(raw):
    assert ResendClient._classify(200, {}, raw).kind == "server_error"


def test_quota_headers_are_numbers_or_nothing():
    for value in ("abc", "-1", "1e3", "", "99999999999999"):
        outcome = ResendClient._classify(200, {"x-resend-daily-quota": value}, b'{"id": "em_1"}')
        assert outcome.daily_used is None or outcome.daily_used <= 10 ** 9


def test_the_outcome_kinds_are_the_fixed_set():
    assert set(resend.KINDS) == set(resend.DEFINITIVE) | set(resend.UNKNOWN) | {"accepted"}
    assert not set(resend.DEFINITIVE) & set(resend.UNKNOWN)
