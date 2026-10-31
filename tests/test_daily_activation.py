"""Data publication on temporary files with host operations as explicit stand-ins.

No Docker, network, service, production file or live credential is used here.
"""
import json
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from cricket_refresh import activation as a
from cricket_refresh import daily

HOST = Path(__file__).resolve().parents[1] / "deploy/host"


class HostStub:
    def __init__(self, root):
        self.root, self.calls, self.fail = root, [], None

    def event(self, name, identity=None):
        self.calls.append((name, identity))
        if self.fail == (name, identity) or self.fail == name:
            self.fail = None
            raise ValueError("injected host failure")

    def api_up(self, identity): self.event("api", identity)
    def smoke(self, identity): self.event("smoke", identity)
    def purge(self): self.event("purge")
    def public_check(self, identity): self.event("public", identity)
    def publish(self, status, identity, activated=None): self.event(status, identity)
    def cleanup(self): self.event("cleanup")


@pytest.fixture
def transaction(tmp_path):
    root = tmp_path / "srv"
    releases = root / "releases"
    releases.mkdir(parents=True)
    for identity in ("release-old", "release-live", "release-next", "release-other"):
        (releases / identity).mkdir()
    a.point(root, "current", "release-live")
    a.point(root, "previous", "release-old")
    host = HostStub(root)
    return a.Transaction(root, host), host


def test_success_records_both_pointers_and_all_post_switch_checks(transaction):
    tx, host = transaction
    tx.start("release-next")
    assert a.current(tx.root) == "release-next"
    assert a.current(tx.root, "previous") == "release-live"
    assert a.read_json(tx.path)["phase"] == "committed"
    assert host.calls == [("api", "release-next"), ("smoke", "release-next"), ("cache_pending", "release-next"),
                          ("purge", None), ("public", "release-next"), ("activated", "release-next"), ("cleanup", None)]


@pytest.mark.parametrize("failure", [("api", "release-next"), ("smoke", "release-next")])
def test_api_failure_restores_both_original_pointers_and_purges_again(transaction, failure):
    tx, host = transaction
    host.fail = failure
    with pytest.raises(ValueError, match="injected"):
        tx.start("release-next")
    assert a.current(tx.root) == "release-live"
    assert a.current(tx.root, "previous") == "release-old"
    assert a.read_json(tx.path)["phase"] == "rolled_back"
    assert host.calls[-5:] == [("api", "release-live"), ("smoke", "release-live"), ("purge", None),
                             ("public", "release-live"), ("rolled_back", "release-live")]
    assert not any(name == "activated" for name, _ in host.calls)


@pytest.mark.parametrize("failure", ["purge", ("public", "release-next")])
def test_cache_failure_is_pending_and_resume_never_restarts_the_api(transaction, failure):
    tx, host = transaction
    host.fail = failure
    with pytest.raises(ValueError):
        tx.start("release-next")
    assert a.current(tx.root) == "release-next"
    assert a.read_json(tx.path)["phase"] == "cache_pending"
    assert not any(name == "activated" for name, _ in host.calls)
    before = list(host.calls)
    tx.resume()
    assert not any(name == "api" for name, _ in host.calls[len(before):])
    assert a.read_json(tx.path)["phase"] == "committed"


@pytest.mark.parametrize("phase", ["ready", "switched", "recovering", "needs_recovery"])
def test_interrupted_switch_cannot_resume_as_if_it_passed(transaction, phase):
    tx, host = transaction
    tx.record({"from": "release-live", "previous": "release-old", "to": "release-next"}, phase)
    with pytest.raises(ValueError, match="recover"):
        tx.resume()
    with pytest.raises(ValueError, match="unfinished"):
        tx.start("release-next")
    assert host.calls == []
    tx.recover()
    assert a.read_json(tx.path)["phase"] == "rolled_back"


@pytest.mark.parametrize("failure", ["api", "smoke", "purge", "public"])
def test_failed_recovery_never_claims_success(transaction, failure):
    tx, host = transaction
    host.fail = failure
    value = {"from": "release-live", "previous": "release-old", "to": "release-next"}
    tx.record(value, "switched")
    with pytest.raises(ValueError):
        tx.recover()
    assert a.read_json(tx.path)["phase"] == "needs_recovery"
    assert not any(name == "rolled_back" for name, _ in host.calls)


def test_recovery_does_not_overwrite_an_unrelated_manual_release(transaction):
    tx, host = transaction
    tx.record({"from": "release-live", "previous": "release-old", "to": "release-next"}, "ready")
    a.point(tx.root, "current", "release-other")
    with pytest.raises(ValueError, match="outside"):
        tx.recover()
    assert a.current(tx.root) == "release-other" and host.calls == []


@pytest.fixture
def installed(tmp_path, dataset):
    root = tmp_path / "srv"
    source = root / "releases/release-live"
    (source / "config/host").mkdir(parents=True)
    (source / "config/compose.yaml").write_text("services: {}\n")
    (source / "config/host/example.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "notifier.toml").write_text('recipient="owner@example.invalid"\n')
    meta = json.loads((dataset / "meta.json").read_text())
    version = meta["data_version"]
    data = root / "data" / version
    shutil.copytree(dataset, data)
    manifest = {"release_id": source.name, "git": {"commit": "a" * 40, "dirty": False},
        "images": {kind: {"tag": f"cricket-{kind}:release-live", "fingerprint": "f" * 64} for kind in ("api", "notifier")},
        "data": {"version": version, "meta_sha256": daily.file_hash(data / "meta.json"),
                 "tables": {table["file"]: table["sha256"] for table in meta["tables"].values()}},
        "golden": {"/v1/meta": {"etag": '"old"', "bytes": 2}}, "files": {"config.tar.gz": "original"}}
    daily.atomic_json(source / "manifest.json", manifest)
    env = {"RELEASE_ID": source.name, "API_IMAGE": "cricket-api:release-live", "NOTIFIER_IMAGE": "cricket-notifier:release-live",
           "DATA_DIR": str(data), "CONFIG_DIR": str(source / "config"), "NOTIFIER_CONFIG_FILE": str(source / "notifier.toml")}
    (source / "release.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    module = a.runtime_module(HOST)
    pool = module.install(source, root)
    return SimpleNamespace(root=root, source=source, meta=meta, data=data, pool=pool, module=module)


def test_shared_config_is_content_addressed_and_tampering_is_refused(installed):
    p = installed
    assert (p.source / "config").is_symlink()
    assert a.environment(p.source)["CONFIG_DIR"] == str(p.pool / "config")
    assert p.module.install(p.source, p.root) == p.pool
    assert a.verified_release(p.root, p.source.name, HOST)[0]["release_id"] == p.source.name
    (p.pool / "config/compose.yaml").write_text("changed")
    with pytest.raises(ValueError, match="differs"):
        a.verified_release(p.root, p.source.name, HOST)


def test_data_release_has_new_manifest_but_same_images_and_config_bind_paths(installed):
    p = installed
    before = {path.name: path.read_bytes() for path in p.source.iterdir() if path.is_file()}
    prepared = {"data_version": p.meta["data_version"], "prepared_utc": "2026-10-28T04:00:00Z"}
    golden = {"/v1/meta": {"etag": '"new"', "bytes": 123}}
    identity = a.install_release(p.root, p.source, p.data, prepared, golden)
    manifest, env = a.verified_release(p.root, identity, HOST)
    assert manifest["kind"] == "data-only" and manifest["golden"] == golden
    assert "files" not in manifest and manifest["origin_bundles"] == {"config.tar.gz": "original"}
    assert manifest["data"]["tables"] == {table["file"]: table["sha256"] for table in p.meta["tables"].values()}
    for key in ("CONFIG_DIR", "NOTIFIER_CONFIG_FILE", "API_IMAGE", "NOTIFIER_IMAGE"):
        assert env[key] == a.environment(p.source)[key]
    assert {path.name: path.read_bytes() for path in p.source.iterdir() if path.is_file()} == before


def test_config_retention_preserves_both_pointer_dependencies_and_unknown_folders(installed):
    p = installed
    a.point(p.root, "current", p.source.name)
    a.point(p.root, "previous", p.source.name)
    unknown = p.root / "runtime-configs/unknown"
    unknown.mkdir()
    p.module.prune(p.root)
    assert p.pool.exists() and unknown.exists()


@pytest.fixture
def candidate(tmp_path, dataset):
    work = tmp_path / "work"
    meta = json.loads((dataset / "meta.json").read_text())
    version = meta["data_version"]
    source = work / "candidates" / version
    shutil.copytree(dataset, source)
    target = tmp_path / "root-copy"
    target.mkdir()
    expected = {"data_version": version, "meta_sha256": daily.file_hash(source / "meta.json")}
    return SimpleNamespace(work=work, source=source, target=target, expected=expected)


def test_import_snapshots_only_regular_files_and_checks_hashes(candidate):
    p = candidate
    a.import_candidate(p.work, p.target, p.expected)
    assert sorted(p.name for p in p.target.iterdir()) == sorted(p.name for p in p.source.iterdir())
    assert (p.target / "meta.json").read_bytes() == (p.source / "meta.json").read_bytes()


@pytest.mark.parametrize("bad", ["extra", "symlink", "folder-link", "corrupt", "metadata-path", "version"])
def test_untrusted_candidate_cannot_escape_or_skip_verification(candidate, bad):
    p = candidate
    if bad == "extra":
        (p.source / "extra.txt").write_text("extra")
    elif bad == "symlink":
        (p.source / "matches.parquet").unlink()
        (p.source / "matches.parquet").symlink_to(p.source / "balls.parquet")
    elif bad == "folder-link":
        moved = p.source.with_name("moved")
        p.source.rename(moved)
        p.source.symlink_to(moved, target_is_directory=True)
    elif bad == "corrupt":
        with (p.source / "matches.parquet").open("ab") as handle:
            handle.write(b"corruption")
    else:
        meta = json.loads((p.source / "meta.json").read_text())
        if bad == "metadata-path":
            meta["tables"]["matches"]["file"] = "../../outside"
        else:
            meta["data_version"] = "aaaaaaaaaaaa"
        (p.source / "meta.json").write_text(json.dumps(meta))
        p.expected["meta_sha256"] = daily.file_hash(p.source / "meta.json")
    with pytest.raises(Exception):
        a.import_candidate(p.work, p.target, p.expected)


def test_stale_preparation_never_reaches_the_image_probe(tmp_path, monkeypatch):
    host = a.Host.__new__(a.Host)
    host.work = tmp_path
    host.op = {"status": "failed", "last_attempt_utc": "2026-10-28T04:00:00Z"}
    daily.atomic_json(tmp_path / "prepared.json", {"data_version": "aaaaaaaaaaaa", "baseline_data_version": "bbbbbbbbbbbb",
        "attempt_utc": "2026-10-27T04:00:00Z"})
    host.command = lambda *args, **kwargs: pytest.fail("probe started for stale data")
    with pytest.raises(ValueError, match="stale"):
        host.prepare_release({"data": {"version": "bbbbbbbbbbbb"}})


def test_plan_writes_nothing_and_does_not_read_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "load_policy", lambda *args: pytest.fail("read policy"))
    assert a.main(["--policy", str(tmp_path / "not-created")]) == 0
    assert list(tmp_path.iterdir()) == []


def test_root_publisher_ignores_builder_supplied_activation_fields(tmp_path):
    host = a.Host.__new__(a.Host)
    host.root, host.public = tmp_path, tmp_path / "public/status.json"
    host.op = {"last_attempt_utc": "2026-10-28T04:00:00Z", "last_successful_check_utc": "2026-10-28T04:01:00Z",
               "last_successful_refresh_utc": "2099-01-01T00:00:00Z", "active_data_version": "aaaaaaaaaaaa"}
    daily.atomic_json(tmp_path / "releases/release-live/manifest.json", {"data": {"version": "bbbbbbbbbbbb"}})
    host.publish("unchanged", "release-live")
    result = a.read_json(host.public)
    assert result["last_successful_refresh_utc"] is None
    assert result["active_data_version"] == "bbbbbbbbbbbb" and result["mode"] == "automatic"


def test_api_up_can_only_recreate_the_api(installed, monkeypatch):
    p = installed
    host = a.Host.__new__(a.Host)
    host.root, host.scripts = p.root, HOST
    calls = []
    host.command = lambda command: calls.append([str(v) for v in command])
    host.api_up(p.source.name)
    assert len(calls) == 1
    assert calls[0][-7:] == ["up", "--detach", "--no-deps", "--wait", "--wait-timeout", "180", "api"]
    assert "systemctl" not in calls[0] and "--remove-orphans" not in calls[0]


def test_prepared_snapshot_activates_with_real_api_metadata_and_rolls_back(installed, tmp_path, make_client):
    import duckdb

    p = installed
    a.point(p.root, "current", p.source.name)
    a.point(p.root, "previous", p.source.name)
    work = tmp_path / "work"
    source = tmp_path / "export"
    shutil.copytree(p.data, source)
    rewritten = tmp_path / "players.parquet"
    with duckdb.connect() as con:
        con.execute("COPY (SELECT * REPLACE ('Updated name' AS name) FROM read_parquet($source)) TO $dest (FORMAT PARQUET)",
                    {"source": str(source / "players.parquet"), "dest": str(rewritten)})
    rewritten.replace(source / "players.parquet")
    meta = json.loads((source / "meta.json").read_text())
    meta["tables"]["players"].update(sha256=daily.file_hash(source / "players.parquet"), bytes=(source / "players.parquet").stat().st_size)
    meta["data_version"] = hashlib.sha256("".join(table["sha256"] for table in meta["tables"].values()).encode()).hexdigest()[:12]
    meta["built_at_utc"] = "2026-10-28 04:00:00"
    daily.atomic_json(source / "meta.json", meta)
    candidate = work / "candidates" / meta["data_version"]
    candidate.parent.mkdir(parents=True)
    source.rename(candidate)
    attempt = {"status": "prepared", "attempt_id": "a" * 32, "last_attempt_utc": "2026-10-28T04:00:00Z",
               "last_successful_check_utc": "2026-10-28T04:01:00Z"}
    daily.atomic_json(work / "prepared.json", {"data_version": meta["data_version"], "baseline_data_version": p.meta["data_version"],
        "prepared_utc": "2026-10-28T04:02:00Z", "attempt_utc": attempt["last_attempt_utc"], "attempt_id": attempt["attempt_id"],
        "meta_sha256": daily.file_hash(candidate / "meta.json")})
    host = a.Host.__new__(a.Host)
    host.root, host.work, host.public, host.scripts, host.op = p.root, work, tmp_path / "public/status.json", HOST, attempt
    events = []

    def probe(command, **kwargs):
        assert Path(command[1]).name == "data_probe.py"
        data = Path(command[command.index("--data") + 1])
        api = make_client(data_dir=data)
        response = api.get("/v1/meta")
        events.append("isolated-probe-stand-in")
        return json.dumps({"data_version": api.get("/status").json()["data_version"],
                           "golden": {"/v1/meta": {"etag": response.headers["etag"], "bytes": len(response.content)}}})

    host.command = probe
    target = host.prepare_release(a.read_json(p.source / "manifest.json"))
    live = []

    def api_up(identity):
        _, env = a.verified_release(p.root, identity, HOST)
        live[:] = [make_client(data_dir=Path(env["DATA_DIR"]), refresh_status_file=host.public)]
        events.append("api-restarted")

    def check(identity):
        manifest = a.read_json(p.root / "releases" / identity / "manifest.json")
        assert live[0].get("/status").json()["data_version"] == manifest["data"]["version"]
        if manifest.get("kind") == "data-only":
            assert live[0].get("/v1/meta").headers["etag"] == manifest["golden"]["/v1/meta"]["etag"]
        events.append("checked")

    host.api_up, host.smoke, host.public_check = api_up, check, check
    host.purge = lambda: events.append("purge-stand-in")
    tx = a.Transaction(p.root, host)
    tx.start(target)
    result = live[0].get("/freshness").json()
    assert result["data_version"] == meta["data_version"]
    assert result["refresh"]["status"] == "activated" and result["refresh"]["last_successful_refresh_utc"]
    page = live[0].get("/docs").text
    assert meta["data_version"] in page and "2026-10-28T04:01:00Z" in page
    assert "Dataset v3" not in page  # No invented friendly registry label for a new hash.
    tx.start(a.current(p.root, "previous"), operation="rollback")
    assert live[0].get("/status").json()["data_version"] == p.meta["data_version"]
    assert live[0].get("/freshness").json()["refresh"]["status"] == "rolled_back"
    assert events.count("api-restarted") == 2 and events.count("purge-stand-in") == 2
    assert a.current(p.root, "previous") == target


def test_failure_record_keeps_pending_distinct_from_a_failed_preparation(transaction):
    tx, host = transaction
    tx.record({"from": "release-live", "previous": "release-old", "to": "release-next"}, "cache_pending")
    with pytest.raises(ValueError):
        with a.record_failure(host, tx):
            raise ValueError("failure")
    assert host.calls == [("cache_pending", "release-live")]


def test_retention_reconciles_only_root_recorded_orphans(installed, tmp_path):
    p = installed
    a.point(p.root, "current", p.source.name)
    a.point(p.root, "previous", p.source.name)
    host = a.Host.__new__(a.Host)
    host.root, host.scripts = p.root, HOST
    orphan = p.root / "data/aaaaaaaaaaaa"
    shutil.copytree(p.data, orphan)
    unknown = p.root / "data/bbbbbbbbbbbb"
    shutil.copytree(p.data, unknown)
    stage = p.root / "data/.candidate-owned"
    stage.mkdir()
    unknown_stage = p.root / "data/.candidate-notowned"
    unknown_stage.mkdir()
    host.remember(version=orphan.name, checksum=daily.file_hash(orphan / "meta.json"), staging=stage)
    host.cleanup()
    assert not orphan.exists() and not stage.exists()
    assert unknown.exists() and unknown_stage.exists() and p.data.exists() and p.pool.exists()
    assert host.owned_data() == {"versions": {}, "staging": []}


def test_tampered_owned_data_stops_cleanup_instead_of_widening_it(installed):
    p = installed
    a.point(p.root, "current", p.source.name)
    a.point(p.root, "previous", p.source.name)
    host = a.Host.__new__(a.Host)
    host.root, host.scripts = p.root, HOST
    orphan = p.root / "data/aaaaaaaaaaaa"
    shutil.copytree(p.data, orphan)
    host.remember(version=orphan.name, checksum="f" * 64)
    with pytest.raises(ValueError, match="metadata changed"):
        host.cleanup()
    assert orphan.exists() and p.data.exists()


@pytest.mark.parametrize("fail", [False, True])
def test_isolated_probe_has_limits_a_watchdog_and_cleanup(monkeypatch, fail):
    import importlib.util
    import subprocess
    spec = importlib.util.spec_from_file_location("test_data_probe", HOST / "data_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "exec" and args[4] == probe.PRESERVATION and fail:
            raise subprocess.CalledProcessError(1, ["docker", *args])
        answers = {"/status": {"data_version": "a" * 12}, "/v1/meta": {"etag": '"new"', "bytes": 10}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(answers))

    monkeypatch.setattr(probe, "command", command)
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            probe.probe("cricket-api:approved", "/staged", "/baseline", ["/v1/meta"])
    else:
        assert probe.probe("cricket-api:approved", "/staged", "/baseline", ["/v1/meta"])["data_version"] == "a" * 12
    run = calls[0]
    for flag, value in (("--network", "none"), ("--memory", "512m"), ("--memory-swap", "512m"), ("--cpus", "0.5"), ("--user", "10001:10001")):
        assert run[run.index(flag) + 1] == value
    assert "--read-only" in run and "/usr/bin/timeout" in run and "300s" in run
    assert "/staged:/data:ro" in run and "/baseline:/baseline:ro" in run
    assert calls[-1][:2] == ["rm", "--force"] and calls[-1][2] == run[run.index("--name") + 1]


def test_publication_guard_refuses_low_disk_and_low_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(a.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    assert a.activation_guard(tmp_path) == "free_disk"
    monkeypatch.setattr(a.shutil, "disk_usage", lambda path: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr(a.sys, "platform", "linux")
    monkeypatch.setattr(a.Path, "read_text", lambda path: "MemAvailable: 1000000 kB\n")
    assert a.activation_guard(tmp_path, minimum_mib=1500) == "host_memory"
    assert a.activation_guard(tmp_path, minimum_mib=600) is None


def test_unreadable_memory_sensor_is_not_treated_as_a_safe_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(a.shutil, "disk_usage", lambda path: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr(a.sys, "platform", "linux")
    monkeypatch.setattr(a.Path, "read_text", lambda path: "unexpected input")
    with pytest.raises(ValueError):
        a.activation_guard(tmp_path)


# The evidence the API needs before it calls the refresh automatic ------------------------------------------------------------------

INVOCATION = "0123456789abcdef0123456789abcdef"
TIMER_ON = {"UnitFileState": "enabled", "ActiveState": "active"}
SERVICE_SCHEDULED = {"InvocationID": INVOCATION, "TriggeredBy": "cricket-data-prepare.timer", "Result": "success", "ActiveState": "inactive"}


def shown(facts):
    return "" if facts is None else "".join(f"{k}={v}\n" for k, v in facts.items())


def publisher(tmp_path, timer=TIMER_ON, service=SERVICE_SCHEDULED, previous=None, **record):
    """A publisher whose systemctl answers come from `timer` and `service` (None or an exception means systemd could not be read)."""
    host = a.Host.__new__(a.Host)
    host.root, host.public = tmp_path, tmp_path / "public/status.json"
    host.op = {"status": "unchanged", "invocation_id": INVOCATION, "last_attempt_utc": "2026-10-28T04:00:00Z", "last_successful_check_utc": "2026-10-28T04:01:00Z", **record}
    daily.atomic_json(tmp_path / "releases/release-live/manifest.json", {"data": {"version": "bbbbbbbbbbbb"}})
    calls = []

    def command(args, **kw):
        calls.append(args)
        answer = timer if "cricket-data-prepare.timer" in args else service
        if isinstance(answer, Exception):
            raise answer
        return shown(answer)
    host.command = command
    if previous:
        daily.atomic_json(host.public, previous)
    return host, calls


def written(host):
    return a.read_json(host.public)


def test_a_successful_scheduled_check_that_found_nothing_new_counts(tmp_path):
    host, calls = publisher(tmp_path)
    host.publish("unchanged", "release-live")
    result = written(host)
    assert result["last_scheduled_check_utc"] == "2026-10-28T04:01:00Z", "a no-op check is a successful scheduled check, and it need not publish anything"
    assert result["publication_verified_utc"] is None
    assert ["systemctl", "show", "cricket-data-prepare.service", "--property=InvocationID", "--property=TriggeredBy", "--property=Result", "--property=ActiveState"] in calls


def test_a_scheduled_run_that_prepared_new_data_counts_too(tmp_path):
    host, _ = publisher(tmp_path, status="prepared")
    host.publish("cache_pending", "release-live")
    assert written(host)["last_scheduled_check_utc"] == "2026-10-28T04:01:00Z"


@pytest.mark.parametrize("service", [
    {**SERVICE_SCHEDULED, "TriggeredBy": ""},                                                        # started by a person
    {**SERVICE_SCHEDULED, "TriggeredBy": "cricket-data-prepare.path"},                              # started by something else
    {**SERVICE_SCHEDULED, "Result": "exit-code"}, {**SERVICE_SCHEDULED, "Result": "timeout"},     # the run failed
    {**SERVICE_SCHEDULED, "ActiveState": "active"}, {**SERVICE_SCHEDULED, "ActiveState": "failed"},
    {**SERVICE_SCHEDULED, "InvocationID": "f" * 32},                                                 # a different, newer run than the one that wrote the record
    {**SERVICE_SCHEDULED, "InvocationID": ""}, {"TriggeredBy": "cricket-data-prepare.timer"}, None, ValueError("systemctl failed")])
def test_a_check_is_scheduled_only_when_systemd_ties_it_to_a_successful_timer_run(tmp_path, service):
    host, _ = publisher(tmp_path, service=service)
    host.publish("unchanged", "release-live")
    assert written(host)["last_scheduled_check_utc"] is None


@pytest.mark.parametrize("record", [{"status": "failed"}, {"status": "running"}, {"status": "needs_recovery"}, {"invocation_id": None}, {"invocation_id": 5}, {"last_successful_check_utc": None}])
def test_a_failed_or_untied_or_timeless_check_is_not_a_scheduled_success(tmp_path, record):
    host, _ = publisher(tmp_path, **record)
    host.publish("unchanged", "release-live")
    assert written(host)["last_scheduled_check_utc"] is None


def test_a_malformed_check_time_is_refused_outright_as_it_always_was(tmp_path):
    host, _ = publisher(tmp_path, last_successful_check_utc="yesterday")
    with pytest.raises(ValueError):
        host.publish("unchanged", "release-live")


def test_history_is_preserved_when_this_run_is_not_a_scheduled_success(tmp_path):
    previous = {"last_scheduled_check_utc": "2026-10-27T04:01:00Z", "publication_verified_utc": "2026-10-20T04:09:00Z", "active_data_version": "bbbbbbbbbbbb"}
    host, _ = publisher(tmp_path, service={**SERVICE_SCHEDULED, "TriggeredBy": ""}, previous=previous)
    host.publish("unchanged", "release-live")
    result = written(host)
    assert result["last_scheduled_check_utc"] == "2026-10-27T04:01:00Z" and result["publication_verified_utc"] == "2026-10-20T04:09:00Z"


def test_the_scheduler_is_read_fresh_every_time_and_never_carried_forward(tmp_path):
    previous = {"scheduler": {"state": "active", "observed_utc": "2026-10-27T04:00:00Z"}, "last_scheduled_check_utc": "2026-10-27T04:01:00Z", "active_data_version": "bbbbbbbbbbbb"}
    host, _ = publisher(tmp_path, timer=ValueError("systemd cannot be read"), previous=previous)
    host.publish("unchanged", "release-live")
    result = written(host)
    assert result["scheduler"]["state"] == "unknown", "an unreadable timer is unknown, not the last good answer"
    assert result["scheduler"]["observed_utc"] > "2026-10-27T04:00:00Z" and result["last_scheduled_check_utc"] is not None


@pytest.mark.parametrize("timer, expected", [
    (TIMER_ON, "active"),
    ({"UnitFileState": "disabled", "ActiveState": "inactive"}, "disabled"),
    ({"UnitFileState": "enabled", "ActiveState": "inactive"}, "disabled"),
    ({"UnitFileState": "disabled", "ActiveState": "active"}, "disabled"),
    ({"UnitFileState": "masked", "ActiveState": "inactive"}, "disabled"),
    ({"UnitFileState": "enabled"}, "unknown"), ({}, "unknown"), (None, "unknown"), (ValueError("no systemd"), "unknown")])
def test_the_timers_present_state_is_active_only_when_it_is_enabled_and_running(tmp_path, timer, expected):
    host, _ = publisher(tmp_path, timer=timer)
    assert host.scheduler_state() == expected
    host.publish("unchanged", "release-live")
    assert written(host)["scheduler"]["state"] == expected


def test_a_completed_publication_is_recorded_once_and_carried_forward(tmp_path):
    host, _ = publisher(tmp_path)
    host.publish("activated", "release-live", activated="2026-10-28T04:09:00Z")
    assert written(host)["publication_verified_utc"] == "2026-10-28T04:09:00Z"
    host.publish("unchanged", "release-live")
    assert written(host)["publication_verified_utc"] == "2026-10-28T04:09:00Z"


@pytest.mark.parametrize("status", ["unchanged", "failed", "cache_pending", "rolled_back", "needs_recovery"])
def test_only_a_finished_activation_proves_publication(tmp_path, status):
    host, _ = publisher(tmp_path)
    host.publish(status, "release-live")
    assert written(host)["publication_verified_utc"] is None


def test_builder_supplied_evidence_is_ignored(tmp_path):
    host, _ = publisher(tmp_path, service=None, timer=None)
    host.op.update({"publication_verified_utc": "2026-10-28T04:09:00Z", "last_scheduled_check_utc": "2026-10-28T04:03:12Z", "scheduler": {"state": "active", "observed_utc": "2026-10-28T04:00:00Z"},
                    "mode": "automatic"})
    host.publish("unchanged", "release-live")
    result = written(host)
    assert result["publication_verified_utc"] is None and result["last_scheduled_check_utc"] is None and result["scheduler"]["state"] == "unknown"


def test_the_preparer_records_systemds_invocation_id_and_nothing_else_for_it(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", INVOCATION)
    assert daily.invocation_id() == INVOCATION
    for bad in ("", "short", INVOCATION.upper(), INVOCATION + "0", "g" * 32, "0123456789abcdef-123456789abcdef"):
        monkeypatch.setenv("INVOCATION_ID", bad)
        assert daily.invocation_id() is None, bad
    monkeypatch.delenv("INVOCATION_ID")
    assert daily.invocation_id() is None
