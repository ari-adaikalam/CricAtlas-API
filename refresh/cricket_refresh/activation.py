"""A durable, fail-closed data-only release transaction.

The builder has no Docker access. This module runs only in a separately approved
root service, using a root-owned policy and installed code. Plan is the default.
No installed release, data folder, image, firewall or monitoring config is edited.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cricket_api.db import load_meta
from cricket_api.freshness import utc_time
from cricket_refresh import bundle
from cricket_refresh.daily import VERSION, atomic_json, file_hash, now, sync_directory
from cricket_refresh.locking import RunLock

TERMINAL = {"committed", "rolled_back"}
MAX_DATA_BYTES = 512 * 1024 ** 2
MAX_JSON = 1024 ** 2
ROOT = Path("/srv/cricket")
BUNDLE_ROOT = bundle.ROOT


def activation_guard(root, minimum_mib=600):
    if shutil.disk_usage(root).free < 8 * 1024 ** 3:
        return "free_disk"
    if sys.platform == "linux":
        readings = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(readings["MemAvailable"].split()[0]) < minimum_mib * 1024:
            return "host_memory"
    return None


def read_json(path, maximum=MAX_JSON):
    # A builder cannot turn a privileged file read into a symlink traversal.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("expected a regular JSON file")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("JSON file is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def release_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._-]{8,80}", value) is None:
        raise ValueError("invalid release ID")
    return value


def current(root, name="current"):
    link = root / "releases" / name
    target = link.resolve(strict=True)
    if not link.is_symlink() or target.parent != root / "releases" or target.is_symlink():
        raise ValueError("release pointer leaves its release directory")
    return release_id(target.name)


def point(root, name, target):
    link = root / "releases" / name
    temporary = link.with_name(f".{name}.{uuid.uuid4().hex}")
    temporary.symlink_to(root / "releases" / release_id(target))
    try:
        os.replace(temporary, link)
        sync_directory(link.parent)
    finally:
        temporary.unlink(missing_ok=True)


def environment(release):
    values = {}
    for line in (release / "release.env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values or "\x00" in value or "\r" in value:
            raise ValueError("invalid release environment")
        values[key] = value
    return values


def runtime_module(host):
    spec = importlib.util.spec_from_file_location("cricket_runtime_store", host / "runtime_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_release(root, identity, host):
    release = root / "releases" / release_id(identity)
    manifest, env = read_json(release / "manifest.json"), environment(release)
    if manifest["release_id"] != identity or env["RELEASE_ID"] != identity:
        raise ValueError("release identity differs")
    version = manifest["data"]["version"]
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise ValueError("invalid data version")
    data = root / "data" / version
    if env["DATA_DIR"] != str(data) or data.is_symlink():
        raise ValueError("release data path differs")
    meta = load_meta(data, verify=True)
    if meta["data_version"] != version or file_hash(data / "meta.json") != manifest["data"]["meta_sha256"]:
        raise ValueError("release metadata differs")
    shared = Path(env["CONFIG_DIR"]).parent
    runtime_module(host).validate(shared, root)
    if (Path(env["CONFIG_DIR"]) != shared / "config" or
            env["NOTIFIER_CONFIG_FILE"] != str(shared / "notifier.toml") or
            (release / "config").resolve() != shared / "config" or
            (release / "notifier.toml").resolve() != shared / "notifier.toml"):
        raise ValueError("data-only activation requires the approved shared config bootstrap")
    for kind in ("api", "notifier"):
        if env[f"{kind.upper()}_IMAGE"] != manifest["images"][kind]["tag"]:
            raise ValueError("release image differs")
    return manifest, env


def import_candidate(work, target, expected):
    """Snapshot unprivileged output before verification, without following links.

    Directory handles pin the candidate even if the builder renames its folder.
    Every file is opened relative to that handle with O_NOFOLLOW. A concurrent
    content change fails the root-owned copy's final hash verification.
    """
    version = expected["data_version"]
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise ValueError("invalid candidate version")
    parent_fd = os.open(work / "candidates", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        folder_fd = os.open(version, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    total = 0
    try:
        names = os.listdir(folder_fd)
        if not names or len(names) > 64 or "meta.json" not in names:
            raise ValueError("unexpected candidate file set")
        for name in names:
            if re.fullmatch(r"(?:meta\.json|[a-z][a-z0-9_]*\.parquet)", name) is None:
                raise ValueError("unexpected candidate file name")
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
            with os.fdopen(descriptor, "rb") as source, (target / name).open("xb") as dest:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DATA_BYTES:
                    raise ValueError("candidate contains a special or oversized file")
                while chunk := source.read(1024 ** 2):
                    total += len(chunk)
                    if total > MAX_DATA_BYTES:
                        raise ValueError("candidate exceeds its publication size bound")
                    dest.write(chunk)
                dest.flush()
                os.fsync(dest.fileno())
            (target / name).chmod(0o644)
    finally:
        os.close(folder_fd)
    if file_hash(target / "meta.json") != expected["meta_sha256"]:
        raise ValueError("candidate metadata differs from preparation")
    raw_meta = read_json(target / "meta.json")
    # load_meta expects trusted exporter paths. Validate them before asking it
    # to open any file as root, not after a malicious path has been followed.
    files = [table["file"] for table in raw_meta["tables"].values()]
    if len(set(files)) != len(files) or set(files) != set(names) - {"meta.json"}:
        raise ValueError("candidate metadata names files outside its snapshot")
    meta = load_meta(target, verify=True)
    if meta["data_version"] != version:
        raise ValueError("candidate data version differs")
    wanted = {"meta.json", *(table["file"] for table in meta["tables"].values())}
    if set(names) != wanted:
        raise ValueError("candidate contains unrecorded files")
    derived = hashlib.sha256("".join(table["sha256"] for table in meta["tables"].values()).encode()).hexdigest()[:12]
    if derived != version:
        raise ValueError("data version is not the hash of the recorded table hashes")
    target.chmod(0o755)


def install_release(root, source, candidate, prepared, golden):
    identity = f"data-{now().replace('-', '').replace(':', '')}-{prepared['data_version']}-{uuid.uuid4().hex[:8]}"
    release_id(identity)
    manifest = copy.deepcopy(read_json(source / "manifest.json"))
    manifest["release_id"], manifest["created_utc"] = identity, now()
    # Bundle hashes describe the original build, not a fictional new tarball.
    manifest["origin_bundles"] = manifest.pop("origin_bundles", manifest.pop("files", {}))
    manifest["kind"] = "data-only"
    meta = read_json(candidate / "meta.json")
    manifest["data"] = {**manifest["data"], "version": prepared["data_version"],
                        "built_utc": meta["built_at_utc"], "snapshot_date": meta.get("snapshot_date"),
                        "tables": {table["file"]: table["sha256"] for table in meta["tables"].values()},
                        "meta_sha256": file_hash(candidate / "meta.json")}
    manifest["golden"] = golden
    manifest["data_refresh"] = {"parent_release": source.name, "prepared_utc": prepared["prepared_utc"]}
    env = environment(source)
    env.update(RELEASE_ID=identity, DATA_DIR=str(candidate))
    with tempfile.TemporaryDirectory(prefix=".data-release-", dir=root / "releases") as directory:
        stage = Path(directory)
        stage.chmod(0o755)
        atomic_json(stage / "manifest.json", manifest, 0o644)
        (stage / "release.env").write_text("".join(f"{key}={value}\n" for key, value in env.items()))
        (stage / "release.env").chmod(0o644)
        (stage / "config").symlink_to(env["CONFIG_DIR"], target_is_directory=True)
        (stage / "notifier.toml").symlink_to(env["NOTIFIER_CONFIG_FILE"])
        os.rename(stage, root / "releases" / identity)
        sync_directory(root / "releases")
    return identity


class Transaction:
    """Callbacks perform bounded host checks; journal writes precede the switch."""
    def __init__(self, root, host):
        self.root, self.host = root, host
        self.path = root / "data-activation.json"

    def record(self, value, phase):
        value["phase"], value["updated_utc"] = phase, now()
        atomic_json(self.path, value)

    def pending(self):
        if self.path.exists():
            value = read_json(self.path)
            if value["phase"] not in TERMINAL:
                return value
        return None

    def finish(self, value):
        if current(self.root) != value["to"] or current(self.root, "previous") != value["from"]:
            raise ValueError("release pointers changed during the transaction")
        # A failed purge or public check resumes here, without another restart.
        self.record(value, "cache_pending")
        self.host.publish("cache_pending", value["to"])
        self.host.purge()
        self.host.public_check(value["to"])
        self.host.publish("rolled_back" if value.get("operation") == "rollback" else "activated", value["to"], activated=now())
        self.record(value, "committed")
        self.host.cleanup()

    def start(self, target, operation="activate"):
        if self.pending():
            raise ValueError("an unfinished activation needs --resume or --recover first")
        if operation not in {"activate", "rollback"}:
            raise ValueError("invalid publication operation")
        value = {"from": current(self.root), "previous": current(self.root, "previous"), "to": release_id(target), "operation": operation}
        self.record(value, "ready")
        try:
            point(self.root, "current", target)
            point(self.root, "previous", value["from"])
            self.record(value, "switched")
            self.host.api_up(target)
            self.host.smoke(target)
        except Exception:
            self.recover(value)
            raise
        self.finish(value)

    def resume(self):
        value = self.pending()
        if not value or value["phase"] != "cache_pending":
            raise ValueError("only cache_pending can resume; an interrupted switch needs --recover")
        self.host.smoke(value["to"])
        self.finish(value)

    def recover(self, value=None):
        value = value or self.pending()
        if not value:
            raise ValueError("no unfinished activation to recover")
        # A crash can happen between either link write. Accept only those
        # recorded IDs, never overwrite an unrelated later manual deployment.
        if (current(self.root) not in {value["from"], value["to"]} or
                current(self.root, "previous") not in {value["from"], value["previous"]}):
            raise ValueError("current release is outside this transaction")
        self.record(value, "recovering")
        try:
            point(self.root, "current", value["from"])
            point(self.root, "previous", value["previous"])
            self.host.api_up(value["from"])
            self.host.smoke(value["from"])
            self.host.purge()
            self.host.public_check(value["from"])
            self.host.publish("rolled_back", value["from"])
        except Exception:
            self.record(value, "needs_recovery")
            raise
        self.record(value, "rolled_back")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("unexpected HTTP redirect")


USER_AGENT = "CricAtlasRefresh/1.0"


def request_json(url, *, data=None, headers=None):
    # Cloudflare's edge answers 403 (error 1010) to urllib's default User-Agent on this zone, so the public checks never reached the origin. The publisher names itself instead.
    request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                      headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    with build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON or response.status != 200:
            raise ValueError("unexpected HTTP response")
        return json.loads(raw), response.headers


class Host:
    def __init__(self, policy, host):
        self.policy, self.scripts = policy, host
        self.root = ROOT
        self.work = Path("/srv/cricket-refresh/work")
        self.public = Path("/srv/cricket-refresh/public/status.json")
        self.op = {}
        site = dict(line.partition("=")[::2] for line in Path("/etc/cricket/site.env").read_text().splitlines() if "=" in line)
        if site.get("API_HOSTNAME", "").strip('"') != policy["hostname"]:
            raise ValueError("the purge policy hostname differs from the site's API hostname")

    def read_check(self):
        check = Path("/srv/cricket-refresh/check/status.json")
        self.op = read_json(check, 8192) if check.exists() else {}

    def command(self, args, timeout=240):
        args = [str(arg) for arg in args]
        if not any(Path(arg).name == "data_probe.py" for arg in args):
            return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout).stdout
        # A Docker client's exit alone does not stop its container. Terminate
        # the helper first, so its SIGTERM handler removes its exact probe.
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    output, errors = process.communicate(timeout=2)
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, args, output, errors)
                    return output
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline or activation_guard(self.root):
                        raise ValueError("isolated probe exceeded its resource or deadline guard")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    def check_images(self, manifest):
        for kind in ("api", "notifier"):
            image = manifest["images"][kind]
            if image["fingerprint"] != self.policy[f"{kind}_fingerprint"]:
                raise ValueError("the installed code differs from the approved daily-refresh policy")
            got = self.command(["python3", self.scripts / "image_fingerprint.py", image["tag"]]).strip()
            if got != image["fingerprint"]:
                raise ValueError("installed image differs from the release manifest")

    def api_up(self, identity):
        release = self.root / "releases" / release_id(identity)
        verified_release(self.root, identity, self.scripts)
        # --no-deps is essential: Caddy, monitoring, the portal and Tailscale
        # stay running. Only the API bind mount changes.
        self.command(["docker", "compose", "--project-name", "cricket", "--env-file", "/etc/cricket/site.env",
                      "--env-file", release / "release.env", "-f", release / "config/compose.yaml",
                      "up", "--detach", "--no-deps", "--wait", "--wait-timeout", "180", "api"])

    def smoke(self, identity):
        self.command([self.scripts / "smoke.sh", release_id(identity)])

    def credentials(self):
        path = Path("/etc/cricket/data-refresh-cloudflare.json")
        info = path.stat(follow_symlinks=False)
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise ValueError("purge credentials must be a root-only regular file")
        value = read_json(path, 8192)
        if re.fullmatch(r"[0-9a-f]{32}", value.get("zone_id", "")) is None:
            raise ValueError("invalid Cloudflare zone ID")
        token = value.get("api_token")
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token) is None:
            raise ValueError("invalid Cloudflare purge token")
        return value

    def purge(self):
        credentials = self.credentials()
        result, _ = request_json(f"https://api.cloudflare.com/client/v4/zones/{credentials['zone_id']}/purge_cache",
            data={"hosts": [self.policy["hostname"]]}, headers={"Authorization": f"Bearer {credentials['api_token']}"})
        if result.get("success") is not True:
            raise ValueError("Cloudflare did not confirm the hostname purge")

    def public_check(self, identity):
        manifest = read_json(self.root / "releases" / identity / "manifest.json")
        # The public address people use (the web app's /api) when the policy names one, else the API's own hostname. `hostname` stays the name whose Cloudflare cache is purged.
        base = self.policy.get("public_base") or "https://" + self.policy["hostname"]
        status, _ = request_json(base + "/status")
        if status.get("data_version") != manifest["data"]["version"]:
            raise ValueError("public status names another snapshot")
        meta, headers = request_json(base + "/v1/meta")
        if (meta.get("data", {}).get("data_version") != manifest["data"]["version"] or
                headers.get("ETag") != manifest["golden"]["/v1/meta"]["etag"]):
            raise ValueError("public metadata differs from the activated snapshot")

    def systemd(self, unit, *properties):
        """What systemd itself says about a unit, read by root. Nothing the builder wrote is used. None when it cannot be read."""
        try:
            out = self.command(["systemctl", "show", unit, *[f"--property={name}" for name in properties]])
            return {key: value.strip() for key, _, value in (line.partition("=") for line in out.splitlines()) if _}
        except Exception:
            return None

    def scheduler_state(self):
        """The timer as it is right now: active (enabled and running), disabled (readable, and not both), or unknown (systemd could not be read). Read at every publication,
        never carried forward, so an old good reading cannot stand in for the present."""
        facts = self.systemd("cricket-data-prepare.timer", "UnitFileState", "ActiveState")
        if not facts or "UnitFileState" not in facts or "ActiveState" not in facts:
            return "unknown"
        return "active" if facts["UnitFileState"] == "enabled" and facts["ActiveState"] == "active" else "disabled"

    def scheduled_check(self):
        """The time of the latest successful check, but only when systemd confirms that this very check was made by a run the timer started and that the run succeeded.
        The builder's record names its invocation (systemd's own id for the run), and root compares that with systemd's account of the service's latest run. A manual
        start has no timer behind it, a failed run has no success, and an older run's id does not match a newer record. A check that found nothing new counts, since
        it is still a successful scheduled check. Returns None for anything else."""
        record = self.op
        if record.get("status") not in {"unchanged", "prepared"} or not isinstance(record.get("invocation_id"), str):
            return None
        facts = self.systemd("cricket-data-prepare.service", "InvocationID", "TriggeredBy", "Result", "ActiveState")
        if not facts or facts.get("InvocationID") != record["invocation_id"] or facts.get("Result") != "success" or facts.get("ActiveState") != "inactive":
            return None
        if "cricket-data-prepare.timer" not in facts.get("TriggeredBy", "").split():
            return None
        try:
            return utc_time(record.get("last_successful_check_utc"))
        except (ValueError, TypeError):
            return None

    def publish(self, status, identity, activated=None):
        manifest = read_json(self.root / "releases" / identity / "manifest.json")
        previous = read_json(self.public, 8192) if self.public.exists() else {}
        value = {"schema_version": 1, "mode": "automatic", "status": status,
                 "attempt_id": self.op.get("attempt_id") if isinstance(self.op.get("attempt_id"), str) and
                     re.fullmatch(r"[0-9a-f]{32}", self.op["attempt_id"]) else None,
                 "last_attempt_utc": utc_time(self.op.get("last_attempt_utc", previous.get("last_attempt_utc"))),
                 "last_successful_check_utc": utc_time(self.op.get("last_successful_check_utc", previous.get("last_successful_check_utc"))),
                 "active_data_version": manifest["data"]["version"],
                 "last_successful_refresh_utc": activated or (
                     previous.get("last_successful_refresh_utc") if
                     previous.get("active_data_version") == manifest["data"]["version"] else None),
                 # The evidence the API needs before it calls the refresh automatic, all written here by root. The two times are history and are carried forward:
                 # a publication that finished (cache cleared, public check passed) and the latest successful check that systemd ties to a timer-started run.
                 # The scheduler's state is the present: it is read now, stamped, and never carried forward, so only a current reading can support the claim.
                 "publication_verified_utc": activated if status == "activated" else previous.get("publication_verified_utc"),
                 "last_scheduled_check_utc": self.scheduled_check() or previous.get("last_scheduled_check_utc"),
                 "scheduler": {"state": self.scheduler_state(), "observed_utc": now()}}
        atomic_json(self.public, value, 0o644)

    def owned_data(self):
        path = self.root / "data-refresh-owned.json"
        return read_json(path) if path.exists() else {"versions": {}, "staging": []}

    def remember(self, *, version=None, checksum=None, staging=None, forget_stage=None):
        record = self.owned_data()
        if version is not None:
            record["versions"][version] = checksum
        if staging is not None:
            record["staging"].append(staging.name)
        if forget_stage is not None:
            record["staging"] = [name for name in record["staging"] if name != forget_stage.name]
        atomic_json(self.root / "data-refresh-owned.json", record)

    def cleanup(self):
        keep = {current(self.root), current(self.root, "previous")}
        versions = {read_json(self.root / "releases" / identity / "manifest.json")["data"]["version"] for identity in keep}
        # Only this publisher's superseded data-only releases are eligible.
        # Full releases are retired by the normal, separately approved deploy.
        for release in (self.root / "releases").iterdir():
            if release.is_symlink() or not release.is_dir() or release.name in keep or not (release / "manifest.json").is_file():
                continue
            manifest = read_json(release / "manifest.json")
            if manifest.get("kind") != "data-only" or manifest.get("release_id") != release.name:
                continue
            shutil.rmtree(release)
        referenced = {read_json(p / "manifest.json")["data"]["version"] for p in
            (self.root / "releases").iterdir() if p.is_dir() and not p.is_symlink() and (p / "manifest.json").is_file()}
        owned = self.owned_data()
        for version, checksum in list(owned["versions"].items()):
            if not isinstance(version, str) or VERSION.fullmatch(version) is None:
                raise ValueError("invalid owned-data record")
            if version in versions or version in referenced:
                continue
            data = self.root / "data" / version
            if data.is_symlink():
                raise ValueError("owned data became a symlink; review it manually")
            if data.exists():
                if file_hash(data / "meta.json") != checksum:
                    raise ValueError("owned metadata changed; review it manually")
                load_meta(data, verify=True)
                shutil.rmtree(data)
            del owned["versions"][version]
        for name in owned["staging"]:
            if not isinstance(name, str) or re.fullmatch(r"\.candidate-[A-Za-z0-9_]+", name) is None:
                raise ValueError("invalid owned staging record")
            path = self.root / "data" / name
            if path.is_symlink():
                raise ValueError("owned staging became a symlink; review it manually")
            if path.exists():
                shutil.rmtree(path)
        owned["staging"] = []
        atomic_json(self.root / "data-refresh-owned.json", owned)
        runtime_module(self.scripts).prune(self.root)

    def prepare_release(self, baseline):
        prepared = read_json(self.work / "prepared.json", 8192)
        attempt = self.op.get("attempt_id", "")
        if (not isinstance(attempt, str) or re.fullmatch(r"[0-9a-f]{32}", attempt) is None or
                self.op.get("status") != "prepared" or prepared.get("attempt_id") != attempt or
                prepared.get("attempt_utc") != self.op.get("last_attempt_utc") or
                prepared["baseline_data_version"] != baseline["data"]["version"]):
            raise ValueError("candidate is stale or was not prepared by the latest successful attempt")
        utc_time(prepared["prepared_utc"])
        if prepared["data_version"] == baseline["data"]["version"]:
            raise ValueError("prepared candidate is already live")
        data_root = self.root / "data"
        with tempfile.TemporaryDirectory(prefix=".candidate-", dir=data_root) as temporary:
            candidate = Path(temporary)
            self.remember(staging=candidate)
            import_candidate(self.work, candidate, prepared)
            target = data_root / prepared["data_version"]
            if target.exists() or target.is_symlink():
                if target.is_symlink():
                    raise ValueError("installed candidate is a symlink")
                load_meta(target, verify=True)
                if file_hash(target / "meta.json") != prepared["meta_sha256"]:
                    raise ValueError("same data hash has different canonical metadata")
            else:
                # Record ownership before rename, so a killed publisher can
                # reconcile a verified but not yet referenced data folder.
                self.remember(version=prepared["data_version"], checksum=prepared["meta_sha256"])
                os.rename(candidate, target)
                sync_directory(data_root)
            self.remember(forget_stage=candidate)
        # Probe inside the already approved image, not as root on the host.
        # The probe verifies preservation and records fresh ETags on new data.
        result = json.loads(self.command(["python3", self.scripts / "data_probe.py", "--image", baseline["images"]["api"]["tag"],
            "--data", target, "--baseline", self.root / "data" / baseline["data"]["version"],
            "--routes", *baseline["golden"].keys()], timeout=360))
        if result["data_version"] != prepared["data_version"]:
            raise ValueError("the isolated probe served another snapshot")
        return install_release(self.root, self.root / "releases" / current(self.root), target, prepared, result["golden"])


def check_public_base(base):
    """The public address in the policy, when there is one: https, a lower-case host, at most one short path segment, no port, no trailing slash."""
    if base is not None and (not isinstance(base, str) or re.fullmatch(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?:/[a-z0-9._-]{1,32})?", base) is None):
        raise ValueError("invalid public API address")


def load_policy(path, require_enabled=True):
    info = path.stat(follow_symlinks=False)
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise ValueError("refresh policy must be root-owned and not writable by others")
    value = read_json(path, 8192)
    if value.get("schema_version") != 1 or (require_enabled and value.get("enabled") is not True):
        raise ValueError("automatic activation is not approved in policy")
    if not isinstance(value.get("hostname"), str) or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value["hostname"]) is None:
        raise ValueError("invalid public API hostname")
    check_public_base(value.get("public_base"))
    for kind in ("api", "notifier", "bundle"):
        key = f"{kind}_sha256" if kind == "bundle" else f"{kind}_fingerprint"
        if not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise ValueError("policy must pin the approved image fingerprints and the installed bundle")
    return value


@contextmanager
def record_failure(host, transaction):
    """Only call while holding the deploy lock; do not overwrite another job."""
    try:
        yield
    except Exception:
        try:
            pending = transaction.pending()
            status = "cache_pending" if pending and pending["phase"] == "cache_pending" else (
                "needs_recovery" if pending else "failed")
            host.publish(status, current(host.root))
        except Exception:
            # The private journal and failing service result remain authoritative
            # if the status directory or served release cannot be read.
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--policy", type=Path, default=Path("/etc/cricket/data-refresh.json"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--resume", action="store_true")
    action.add_argument("--recover", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    if not any((args.apply, args.resume, args.recover, args.rollback)):
        print("Plan only. Verify prepared data, probe the approved image, switch the API, purge this hostname and verify public metadata. No changes made.")
        return 0
    try:
        if os.geteuid() != 0:
            raise ValueError("activation requires the separately approved root service")
        # An explicit root recovery/rollback must still work when the owner has
        # disabled scheduling. Image pins, hostname and credentials still apply.
        policy = load_policy(args.policy, require_enabled=args.apply)
        # The code that runs as root is the code the policy names, not whatever is in /opt now.
        bundle.verify(BUNDLE_ROOT, policy["bundle_sha256"])
        host = Host(policy, Path(__file__).resolve().parents[2] / "deploy/host")
        with RunLock(ROOT / "deployment.lock", "deployment"), RunLock(host.work / "refresh.lock", "daily refresh"):
            host.read_check()
            transaction = Transaction(ROOT, host)
            with record_failure(host, transaction):
                manifest, env = verified_release(ROOT, current(ROOT), host.scripts)
                host.check_images(manifest)
                host.credentials()
                if args.recover:
                    transaction.recover()
                elif args.resume:
                    transaction.resume()
                elif transaction.pending():
                    raise ValueError("unfinished transaction; use --resume or --recover")
                elif args.rollback:
                    target = current(ROOT, "previous")
                    old, old_env = verified_release(ROOT, target, host.scripts)
                    host.check_images(old)
                    if any(env[key] != old_env[key] for key in ("CONFIG_DIR", "NOTIFIER_CONFIG_FILE", "API_IMAGE", "NOTIFIER_IMAGE")):
                        raise ValueError("data-only rollback cannot change code or config; use the manual release workflow")
                    transaction.start(target, operation="rollback")
                elif host.op.get("status") in {"unchanged", "failed"}:
                    host.publish(host.op["status"], current(ROOT))
                elif host.op.get("status") != "prepared":
                    # A killed builder can leave its last recorded state as running.
                    # We hold its lock now, so no preparation is still in progress.
                    raise ValueError("no completed preparation to publish")
                else:
                    if activation_guard(ROOT, minimum_mib=1500):
                        raise ValueError("activation requires 8 GiB free and 1500 MiB host available memory")
                    host.cleanup()
                    target = host.prepare_release(manifest)
                    transaction.start(target)
        print("Data refresh operation completed. Read the public freshness record and private transaction journal for its result.")
        return 0
    except Exception as error:
        # Tokens, paths from network errors and response bodies are never printed.
        print(f"Data activation stopped ({type(error).__name__}). No success is claimed; inspect the private journal before retrying.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
