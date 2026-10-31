"""The refresh bundle's record and its checks (cricket_refresh.bundle): what makes the installed code the code the root policy approved."""
import json
import platform
from importlib import metadata
from pathlib import Path

import pytest

from cricket_refresh import activation, bundle

ID = "20261028-0123abcd-89abcdef"


def install(root: Path, **changes) -> Path:
    """An installed layout: bundles/ID/{api,venv,BUNDLE.json} and the three links, as install_refresh_host.sh leaves it."""
    folder = root / "bundles" / ID
    (folder / "api" / "cricket_api").mkdir(parents=True)
    (folder / "api" / "cricket_api" / "db.py").write_text("x = 1\n")
    (folder / "api" / "deploy" / "host").mkdir(parents=True)
    (folder / "api" / "deploy" / "host" / "smoke.sh").write_text("#!/bin/sh\n")
    (folder / "venv").mkdir()
    record = {"schema_version": 1, "bundle_id": ID, "git_commit": "0" * 40, "dirty": False, "created_utc": "2026-10-28T00:00:00Z",
              "python_tag": bundle.tag_of_interpreter(), "machine": platform.machine(), "lock_sha256": "0" * 64,
              "packages": {"pytest": metadata.version("pytest")}, "code": bundle.tree_hashes(folder / "api"), "wheels": {}}
    record.update(changes)
    (folder / "BUNDLE.json").write_bytes(bundle.canonical(record))
    for name, target in (("api", "api"), ("venv", "venv"), ("BUNDLE.json", "BUNDLE.json")):
        (root / name).symlink_to(f"bundles/{ID}/{target}")
    return folder


def identity(folder: Path) -> str:
    return bundle.sha256_file(folder / "BUNDLE.json")


def test_an_installed_bundle_verifies_and_reports_its_identity(tmp_path):
    folder = install(tmp_path)
    assert bundle.verify(tmp_path, identity(folder)) == {"bundle_id": ID, "identity": identity(folder)}
    assert bundle.main(["--verify", "--root", str(tmp_path)]) == 0


def test_a_different_identity_is_refused_even_when_the_files_are_intact(tmp_path):
    install(tmp_path)
    for wrong in ("b" * 64, "not a hash", ""):
        with pytest.raises(ValueError, match="policy approves"):
            bundle.verify(tmp_path, wrong)


def test_changed_missing_and_extra_code_is_found(tmp_path):
    folder = install(tmp_path)
    code = folder / "api" / "cricket_api" / "db.py"
    code.write_text("x = 2\n")
    with pytest.raises(ValueError, match="code differs"):
        bundle.verify(tmp_path)
    code.write_text("x = 1\n")
    bundle.verify(tmp_path)
    (folder / "api" / "cricket_api" / "extra.py").write_text("")
    with pytest.raises(ValueError, match="code differs"):
        bundle.verify(tmp_path)
    (folder / "api" / "cricket_api" / "extra.py").unlink()
    code.unlink()
    with pytest.raises(ValueError, match="code differs"):
        bundle.verify(tmp_path)


def test_a_bytecode_cache_is_not_a_difference_but_a_link_or_special_file_is(tmp_path):
    folder = install(tmp_path)
    (folder / "api" / "cricket_api" / "__pycache__").mkdir()
    (folder / "api" / "cricket_api" / "__pycache__" / "db.cpython-314.pyc").write_bytes(b"\0")
    bundle.verify(tmp_path)
    (folder / "api" / "cricket_api" / "alias.py").symlink_to("db.py")
    with pytest.raises(ValueError, match="link or a special file"):
        bundle.verify(tmp_path)


def test_a_package_version_that_differs_from_the_lock_is_found(tmp_path):
    install(tmp_path, packages={"pytest": "0.0.1"})
    with pytest.raises(ValueError, match="pytest is"):
        bundle.verify(tmp_path)


def test_a_missing_package_is_found(tmp_path):
    install(tmp_path, packages={"no-such-distribution-xyz": "1.0"})
    with pytest.raises(ValueError, match="not installed"):
        bundle.verify(tmp_path)


def test_another_interpreter_or_machine_is_refused(tmp_path):
    install(tmp_path, python_tag="cp399")
    with pytest.raises(ValueError, match="interpreter or machine"):
        bundle.verify(tmp_path)


def test_a_bundle_built_from_uncommitted_files_is_never_installable(tmp_path):
    install(tmp_path, dirty=True)
    with pytest.raises(ValueError, match="uncommitted"):
        bundle.verify(tmp_path)


def test_links_that_leave_the_recorded_bundle_are_refused(tmp_path):
    folder = install(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / "api").unlink()
    (tmp_path / "api").symlink_to(other)
    with pytest.raises(ValueError, match="link api"):
        bundle.verify(tmp_path)
    (tmp_path / "api").unlink()
    (tmp_path / "api").symlink_to(f"bundles/{ID}/api")
    (tmp_path / "BUNDLE.json").unlink()
    (tmp_path / "BUNDLE.json").symlink_to(folder.parent.parent / "outside.json")
    with pytest.raises((ValueError, OSError)):
        bundle.verify(tmp_path)


def test_no_installed_bundle_is_a_clear_refusal_and_a_failed_exit(tmp_path, capsys):
    with pytest.raises(ValueError, match="no installed bundle"):
        bundle.verify(tmp_path)
    assert bundle.main(["--verify", "--root", str(tmp_path)]) == 1
    assert "bundle check failed" in capsys.readouterr().err


def test_without_a_flag_the_command_only_describes_itself(tmp_path, capsys):
    assert bundle.main(["--root", str(tmp_path)]) == 0
    assert "Plan only" in capsys.readouterr().out


def staged(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    (stage / "code" / "api" / "cricket_api").mkdir(parents=True)
    (stage / "code" / "api" / "cricket_api" / "db.py").write_text("x = 1\n")
    (stage / "wheelhouse").mkdir()
    wheel = stage / "wheelhouse" / "six-1.17.0-py2.py3-none-any.whl"
    wheel.write_bytes(b"PK")
    (stage / "requirements.txt").write_text("six==1.17.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")
    record = {"schema_version": 1, "bundle_id": ID, "git_commit": "0" * 40, "dirty": False, "python_tag": "cp314", "machine": "x86_64",
              "lock_sha256": bundle.sha256_file(stage / "requirements.txt"), "packages": {"six": "1.17.0"},
              "code": bundle.tree_hashes(stage / "code" / "api"), "wheels": {wheel.name: bundle.sha256_file(wheel)}}
    (stage / "BUNDLE.json").write_bytes(bundle.canonical(record))
    return stage


def test_a_staged_bundle_is_checked_against_its_own_record_before_anything_is_installed(tmp_path):
    stage = staged(tmp_path)
    assert bundle.verify_stage(stage, identity(stage))["bundle_id"] == ID
    with pytest.raises(ValueError, match="not the one that was built"):
        bundle.verify_stage(stage, "c" * 64)
    (stage / "wheelhouse" / "six-1.17.0-py2.py3-none-any.whl").write_bytes(b"PK changed")
    with pytest.raises(ValueError, match="wheels differ"):
        bundle.verify_stage(stage, identity(stage))


def test_a_staged_bundle_whose_lock_or_code_changed_is_refused(tmp_path):
    stage = staged(tmp_path)
    (stage / "requirements.txt").write_text("six==9.9.9\n")
    with pytest.raises(ValueError, match="lock differs"):
        bundle.verify_stage(stage, identity(stage))
    stage = staged(tmp_path / "second")
    (stage / "code" / "api" / "cricket_api" / "db.py").write_text("x = 2\n")
    with pytest.raises(ValueError, match="code differs"):
        bundle.verify_stage(stage, identity(stage))


# The root policy ------------------------------------------------------------------------------------------------------------------------------

def policy(tmp_path: Path, **changes) -> Path:
    value = {"schema_version": 1, "enabled": True, "hostname": "api.example.test", "api_fingerprint": "a" * 64, "notifier_fingerprint": "b" * 64,
             "bundle_sha256": "c" * 64}
    value.update(changes)
    path = tmp_path / "data-refresh.json"
    path.write_text(json.dumps(value))
    path.chmod(0o644)
    return path


def as_root(monkeypatch):
    real = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, **kw: type("Info", (), {"st_uid": 0, "st_mode": real(self, **kw).st_mode})() if self.name == "data-refresh.json" else real(self, **kw))


def test_the_policy_must_name_the_installed_bundle(tmp_path, monkeypatch):
    as_root(monkeypatch)
    assert activation.load_policy(policy(tmp_path))["bundle_sha256"] == "c" * 64
    for bad in ("", "short", "C" * 64, None):
        with pytest.raises(ValueError, match="installed bundle"):
            activation.load_policy(policy(tmp_path, bundle_sha256=bad))


def test_a_policy_without_the_bundle_key_is_refused_even_for_recovery(tmp_path, monkeypatch):
    as_root(monkeypatch)
    value = json.loads(policy(tmp_path).read_text())
    del value["bundle_sha256"]
    (tmp_path / "data-refresh.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="installed bundle"):
        activation.load_policy(tmp_path / "data-refresh.json", require_enabled=False)


def test_the_example_policy_is_disabled_and_has_every_key_the_loader_reads():
    example = json.loads((Path(__file__).parents[1] / "deploy/host/data-refresh.example.json").read_text())
    assert example["enabled"] is False
    assert {"hostname", "api_fingerprint", "notifier_fingerprint", "bundle_sha256"} <= set(example)


# The publisher actually runs the check ------------------------------------------------------------------------------------------

def run_main(monkeypatch, tmp_path, verify):
    """activation.main with root and the policy faked, and the bundle check replaced by `verify`. Returns (exit code, whether the host was reached)."""
    reached = []
    monkeypatch.setattr(activation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(activation, "load_policy", lambda path, require_enabled=True: {"hostname": "api.example.test", "bundle_sha256": "c" * 64})
    monkeypatch.setattr(activation.bundle, "verify", verify)
    monkeypatch.setattr(activation, "Host", lambda policy, scripts: reached.append("host") or (_ for _ in ()).throw(RuntimeError("stop here, past the check")))
    code = activation.main(["--apply", "--policy", str(tmp_path / "policy.json")])
    return code, bool(reached)


def test_the_publisher_checks_the_installed_bundle_against_the_policy_before_it_touches_the_host(monkeypatch, tmp_path):
    seen = []
    code, reached = run_main(monkeypatch, tmp_path, lambda root, identity: seen.append((root, identity)))
    assert seen == [(activation.BUNDLE_ROOT, "c" * 64)], "the check ran, on the installed root, with the identity the policy names"
    assert reached, "and the run went on to the host only after it"


def test_a_bundle_that_is_not_the_one_the_policy_names_stops_the_publisher_before_the_host(monkeypatch, tmp_path, capsys):
    def refuse(root, identity):
        raise ValueError("the installed bundle is not the one the policy approves")
    code, reached = run_main(monkeypatch, tmp_path, refuse)
    assert code == 1 and not reached
    assert "No success is claimed" in capsys.readouterr().out


def test_recovery_and_rollback_also_require_the_approved_bundle(monkeypatch, tmp_path):
    for flag in ("--recover", "--rollback", "--resume"):
        called = []
        monkeypatch.setattr(activation.os, "geteuid", lambda: 0)
        monkeypatch.setattr(activation, "load_policy", lambda path, require_enabled=True: {"hostname": "api.example.test", "bundle_sha256": "c" * 64})
        monkeypatch.setattr(activation.bundle, "verify", lambda root, identity: called.append(flag) or (_ for _ in ()).throw(ValueError("no")))
        monkeypatch.setattr(activation, "Host", lambda *a: pytest.fail("the host was reached without a verified bundle"))
        assert activation.main([flag, "--policy", str(tmp_path / "p.json")]) == 1 and called == [flag]
