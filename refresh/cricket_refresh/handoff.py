"""The record that carries one preparation from its build stage to its export stage, and the call that starts the export in a fresh interpreter.

The build stage (download, ingest, CSV build) leaves several hundred MiB of memory behind that the export has no use for. A second process started while the first still lives
would not help, because the service's memory limit counts every process in it. So the build stage writes this record and then replaces its own process image with a new
interpreter (exec): the old interpreter and everything it held are gone before the new one runs a line, the process id, the process group and the open run lock stay the same,
and the supervisor above it notices nothing.

The record is private (mode 0600, inside the workspace) and is checked in full before anything in it is used. A record that is missing, altered, from another attempt or from
another service run stops the preparation with the status "failed"; it never lets the export guess.
"""
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
MAX_BYTES = 4 * 1024 * 1024
STAGES = ("build", "export")
NAME = ".handoff.json"


# One value per interpreter: a replaced process image starts a new one, so two stages in the same process id still show different tokens.
INTERPRETER = secrets.token_hex(8)


def self_status(*keys) -> dict:
    """Memory figures of this process in MiB from /proc (Linux), or None where there is no /proc. Reads only."""
    try:
        wanted = {key + ":" for key in keys}
        found = {}
        for line in open("/proc/self/status"):
            name = line.split(None, 1)[0]
            if name in wanted:
                found[name[:-1]] = int(line.split()[1]) // 1024
        return found or None
    except (OSError, ValueError):
        return None


def entry_marker(stage: str, replaced_interpreter=None) -> dict:
    """What a stage reports the moment it starts: which interpreter it is, which one it replaced, and how much memory the process holds. The pid and the start time do not change
    across an exec, so this record, and the memory figure, are what show that a new interpreter is running."""
    status = self_status("RssAnon", "VmHWM", "VmRSS") or {}
    try:
        start_ticks = int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        start_ticks = None
    return {"event": "stage_entry", "stage": stage, "pid": os.getpid(), "start_ticks": start_ticks, "interpreter": INTERPRETER, "replaced_interpreter": replaced_interpreter,
            "rss_anon_mib": status.get("RssAnon"), "rss_mib": status.get("VmRSS"), "peak_rss_mib": status.get("VmHWM"), "modules": len(sys.modules)}


class HandoffError(ValueError):
    """The record is missing, unreadable, altered or does not belong to this run. The message is safe to print."""


def digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write(path: Path, body: dict):
    """Write the record atomically, readable by this account only."""
    document = {"schema_version": SCHEMA, "sha256": digest(body), "body": body}
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def checked_workspace(work_root: Path, workspace) -> Path | None:
    """The workspace folder if it is this preparer's own (a build- folder directly under the work root, not a link, with its marker), else None."""
    try:
        path = Path(workspace)
        if path.is_symlink() or not path.is_dir():
            return None
        if path.resolve().parent != work_root.resolve() or not path.name.startswith("build-") or not (path / ".daily-workspace").is_file():
            return None
        return path
    except (OSError, TypeError, ValueError):
        return None


def read(path: Path, *, stage: str, work_root: Path, attempt_id, invocation) -> dict:
    """The record's body, after every check. Raises HandoffError with a plain reason."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise HandoffError("the handoff record is missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise HandoffError("the handoff record is not a plain file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HandoffError("the handoff record is not private to this account")
    if info.st_size > MAX_BYTES:
        raise HandoffError("the handoff record is too large")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HandoffError("the handoff record cannot be read") from None
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA or not isinstance(document.get("body"), dict):
        raise HandoffError("the handoff record has an unknown format")
    body = document["body"]
    if document.get("sha256") != digest(body):
        raise HandoffError("the handoff record was altered")
    if body.get("stage") != stage or stage not in STAGES:
        raise HandoffError("the handoff record is for another stage")
    if not isinstance(attempt_id, str) or body.get("attempt_id") != attempt_id:
        raise HandoffError("the handoff record belongs to another attempt")
    if body.get("invocation_id") != invocation:
        raise HandoffError("the handoff record belongs to another service run")
    workspace = checked_workspace(work_root, body.get("workspace"))
    if workspace is None or path.resolve().parent != workspace.resolve():
        raise HandoffError("the handoff record names a workspace this preparation does not own")
    if body.get("state_dir") not in (None, str(work_root / "state")):
        raise HandoffError("the handoff record names another state folder")
    if body.get("incremental") not in (True, False):
        raise HandoffError("the handoff record does not say whether kept results are used")
    return body


# A name for the one call that cannot be tried in a test. Tests replace it with a function that runs the next stage in the same process and then raises ProcessReplaced.
_execv = os.execv


class ProcessReplaced(BaseException):
    """Only a test double for exec raises this: the real call does not return."""
    def __init__(self, code=0):
        self.code = code


def replace_process(arguments, keep_fds=()):
    """Start `python arguments...` in place of this process. Never returns, unless the call itself fails (then it raises OSError and nothing has changed).
    The descriptors in `keep_fds` stay open across the call; that is how the run lock stays held."""
    for descriptor in keep_fds:
        os.set_inheritable(descriptor, True)
    sys.stdout.flush()
    sys.stderr.flush()
    _execv(sys.executable, [sys.executable, *arguments])
