"""/status says how old the data is and counts requests. It does not say how full the rate limiter is (finding 10 of the audit): that number tells a caller how close the table is to its bound,
and so how many addresses it would take to fill it. The limiter still keeps its own accounting, which tests of the limiter and of the admission gate rely on."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricket_api import models

FIELDS = {"data_version", "data_built_utc", "snapshot_date", "started_utc", "requests", "client_errors", "server_errors", "rate_limited"}
README = (Path(__file__).parent.parent / "docs" / "reference.md").read_text()


def test_status_has_exactly_the_data_age_and_the_request_counters(client):
    body = client.get("/status").json()
    assert set(body) == FIELDS
    assert all(isinstance(body[k], int) for k in ("requests", "client_errors", "server_errors", "rate_limited"))


def test_nothing_in_status_describes_the_limiter(client):
    body = client.get("/status").json()
    for name in body:
        assert not re.search(r"track|limiter|bucket|overflow|occupan|table|clients?$|addresses", name), name
    assert "tracked_clients" not in json.dumps(body)


def test_the_response_model_has_no_such_field_and_refuses_one():
    assert set(models.Status.model_fields) == FIELDS
    good = dict(data_version="v", data_built_utc="t", snapshot_date=None, started_utc="t", requests=1, client_errors=0, server_errors=0, rate_limited=0)
    models.Status.model_validate(good)
    with pytest.raises(Exception):
        models.Status.model_validate({**good, "tracked_clients": 3})


def test_the_openapi_document_has_no_such_field(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["Status"]
    assert set(schema["properties"]) == FIELDS and set(schema["required"]) == FIELDS
    assert "tracked_clients" not in client.get("/openapi.json").text


def test_the_documentation_page_and_the_readme_do_not_mention_it(client):
    page = client.get("/docs").text
    assert "tracked_clients" not in page and "Tracked Clients" not in page and "tracked clients" not in page.lower()
    assert "tracked_clients" not in README


def test_the_limiter_still_counts_the_clients_inside_the_process_and_status_does_not_show_it(make_client):
    app = make_client().app
    peers = [TestClient(app, client=(f"203.0.113.{i}", 4000), base_url="http://testserver") for i in range(1, 6)]
    for peer in peers:
        peer.get("/v1/meta")
    assert app.state.limiter.clients() == 5, "the accounting is intact"
    assert set(peers[0].get("/status").json()) == FIELDS


def test_the_counters_still_count(client):
    before = client.get("/status").json()
    client.get("/v1/matches/999999")
    client.get("/v1/matches?bogus=1")
    after = client.get("/status").json()
    assert after["requests"] - before["requests"] == 3 and after["client_errors"] - before["client_errors"] == 2


def test_the_route_does_not_read_the_limiter():
    source = (Path(__file__).parent.parent / "cricket_api" / "routes" / "meta.py").read_text()
    status_source = source[source.index("async def status"):source.index("@router.get(\"/v1/meta\"")]
    assert "limiter" not in status_source.replace("How full the limiter is stays inside the process", "")


def test_the_readme_states_the_deployment_requirements_and_the_limits_it_accepts():
    for text in ("## Deployment requirements and known limits", "not root", "read-only", "is in memory, is not read only, and can read and write inside the data folder (accepted)", "restricted: external access is off", "Installing and loading extensions is disabled", "the configuration is locked", "HSTS is configurable, and the value for each stage is a deployment decision", "includeSubDomains",
                 "the finding is not resolved until the deployed header is checked on the real host", "request and connection limits at the proxy or CDN", "responses are compressed", "not part of this release", "405", "413"):
        assert text in README, text
    assert "cannot read or write a file" not in README, "the connection can read and write inside the data folder"


def test_the_audit_gives_each_of_findings_10_to_13_a_disposition_and_does_not_call_12_resolved():
    design = (Path(__file__).parent.parent / "docs" / "api_design.md").read_text()
    section = design[design.index("## Security findings and their dispositions"):]
    for text in ("Fixed: `tracked_clients` removed", "Accepted limitation", "A deployment decision", "Not resolved", "Documented as bounded limitations"):
        assert text in section, text
    assert "| 12 | HSTS uses `includeSubDomains` | A deployment decision. Not resolved" in section and "the one-year value is not set" in section
