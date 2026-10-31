"""The ledger: every message, its immutable payload, its state, and the counters that bound what is sent.

One SQLite file in WAL mode with full synchronous writes, one writer at a time, every change in a single `BEGIN IMMEDIATE` transaction together with the row it belongs to. The ledger is the only thing
that decides what may be sent, and it records that a send is starting before the send happens, so a crash can leave an attempt of unknown outcome but never an unrecorded one.

What it promises, and what it does not (see README.md): a message is stored once (a unique key), its bytes never change, and a repeat of the same key and bytes within Resend's 24 hour window cannot
be accepted twice. It does not promise that a message is sent: during an outage, after the attempts are used up, or if the file is lost, a message may never go out, and those cases end in a visible state.
"""
import contextlib
import json
import os
import random
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from cricket_notifier import budget, payload, states
from cricket_notifier.config import Config
from cricket_notifier.errors import IllegalTransition, PayloadTooLarge, StorageUnavailable
from cricket_notifier.payload import limit_for
from cricket_notifier.resend import ACCEPTED, CONCURRENT, NETWORK_ERROR, PERMANENT, QUOTA_DAILY, QUOTA_MONTHLY, RATE_LIMITED, SERVER_ERROR, CHANGED_PAYLOAD, Outcome

WINDOW_SECONDS = 24 * 3600          # how long Resend keeps an idempotency key
WINDOW_MARGIN = 300                 # a retry is not scheduled in the last five minutes of the window
PAGE = 4096
MAX_CONTEXT_BYTES = 4096
FAILURE_KINDS = (RATE_LIMITED, PERMANENT, SERVER_ERROR, NETWORK_ERROR)       # an attempt that ends like this counts towards opening the circuit breaker
PRIORITY = {"summary": 0, "email_path": 1, "host": 2, "refresh": 2, "suppression": 3, "digest": 4, "alert": 5, "recovery": 5}
DIGEST_OWED_DAYS = 7                  # an incident owed to a digest is kept this long at most
PROBE_KINDS = ("email_path", "summary", "host", "suppression")                # what may go out as the one probe while the breaker is open

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    klass TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    due_at REAL,
    deadline REAL,
    slot TEXT,
    payload BLOB,
    payload_sha256 TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    context TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_attempt_at REAL,
    last_attempt_at REAL,
    next_attempt_at REAL,
    last_outcome TEXT,
    last_error TEXT,
    last_status INTEGER,
    resend_id TEXT,
    accepted_at REAL,
    charge_state TEXT NOT NULL DEFAULT 'reserved',
    charge_day TEXT,
    charge_month TEXT,
    resent_of INTEGER,
    reviewed_at REAL,
    maybe_accepted INTEGER NOT NULL DEFAULT 0,
    digest_owed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_state ON messages(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS messages_charge ON messages(charge_state, charge_day, klass);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bandwidth (hour INTEGER PRIMARY KEY, bytes INTEGER NOT NULL, measured INTEGER NOT NULL);
"""


class StorageRefused(Exception):
    """A low priority write was refused because storage is short. The webhook answers 503 and Alertmanager tries again later."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def utc_month(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def next_utc_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() + 86400


def next_utc_month(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return (datetime(d.year + 1, 1, 1, tzinfo=timezone.utc) if d.month == 12 else datetime(d.year, d.month + 1, 1, tzinfo=timezone.utc)).timestamp()


@dataclass(frozen=True)
class PersistResult:
    id: int
    created: bool
    state: str


@dataclass(frozen=True)
class Attempt:
    id: int
    kind: str
    body: bytes
    sha256: str
    idempotency_key: str
    attempt_no: int
    probe: bool


@dataclass(frozen=True)
class Blocked:
    reason: str


@dataclass(frozen=True)
class Finished:
    state: str
    breaker_event: str | None = None      # "opened", "reopened" or "closed"


@dataclass(frozen=True)
class StorageStatus:
    used_bytes: int
    db_bytes: int
    wal_bytes: int
    free_bytes: int
    mode: str                              # ok, wal_over_target, low_priority_refused, full
    reasons: tuple


class Ledger:
    def __init__(self, config: Config, clock=time.time, free_space=None, rng: random.Random | None = None):
        self.config, self.clock = config, clock
        self.rng = rng or random.Random()
        self._free_space = free_space or (lambda path: shutil.disk_usage(os.path.dirname(path) or ".").free)
        self._lock = threading.RLock()
        self._full = False
        self.checkpoint_blocked_since: float | None = None
        self._last_housekeeping = -1e9
        self.counters = {"checkpoints_blocked": 0, "low_priority_refused": 0, "payload_too_large": 0, "storage_full": 0}
        self.path = config.storage.ledger_path
        self.day, self.month = budget.Limits.day(config.budgets), budget.Limits.month(config.budgets)
        self._conn = self._open()

    # -- the file ------------------------------------------------------------------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        s = self.config.storage
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fresh = not os.path.exists(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row
        if fresh:
            conn.execute(f"PRAGMA page_size={PAGE}")
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA max_page_count={s.max_db_mb * 1024 * 1024 // PAGE}")
        conn.execute(f"PRAGMA journal_size_limit={s.wal_target_mb * 1024 * 1024}")
        conn.execute("PRAGMA wal_autocheckpoint=256")
        conn.executescript(SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        for column in ("maybe_accepted", "digest_owed"):
            if column not in have:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")      # a ledger written before the flag existed
        return conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def txn(self):
        """One write transaction. A database that is full or cannot be written is StorageUnavailable, which says nothing about why, and the caller then sends nothing."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise StorageUnavailable(f"the ledger cannot be written ({type(error).__name__})") from None
            try:
                yield self._conn
                self._conn.execute("COMMIT")
                self._full = False
            except sqlite3.Error as error:
                self._rollback()
                if "full" in str(error).lower():
                    self._full = True
                    self.counters["storage_full"] += 1
                raise StorageUnavailable(f"the ledger cannot be written ({type(error).__name__})") from None
            except BaseException:
                self._rollback()
                raise

    def _rollback(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute("ROLLBACK")

    def read(self, sql: str, args=()) -> list:
        """A short read. Never held across a send or a wait, so it cannot keep a checkpoint from finishing."""
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def read_one(self, sql: str, args=()):
        rows = self.read(sql, args)
        return rows[0] if rows else None

    # -- storage -------------------------------------------------------------------------------------------------------------------
    def _pages(self) -> tuple[int, int]:
        with self._lock:
            return self._conn.execute("PRAGMA page_count").fetchone()[0], self._conn.execute("PRAGMA freelist_count").fetchone()[0]

    def storage_status(self) -> StorageStatus:
        s = self.config.storage
        pages, free_pages = self._pages()
        used = (pages - free_pages) * PAGE
        wal = os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0
        free = self._free_space(self.path)
        reasons = []
        if self._full:
            mode = "full"
            reasons.append("database_full")
        else:
            if wal >= s.wal_refuse_mb * 1024 * 1024:
                reasons.append("wal_over_refusal_threshold")
            if used >= s.max_db_mb * 1024 * 1024 - 256 * 1024:
                reasons.append("database_at_cap")
            if free < s.free_disk_floor_mb * 1024 * 1024:
                reasons.append("free_disk_below_floor")
            if reasons:
                mode = "low_priority_refused"
            elif wal >= s.wal_target_mb * 1024 * 1024:
                mode = "wal_over_target"
            else:
                mode = "ok"
        return StorageStatus(used, pages * PAGE, wal, free, mode, tuple(reasons))

    def checkpoint(self) -> tuple[int, int, int]:
        """Ask for a checkpoint, passive and then truncating. A reader that holds an older snapshot keeps it from finishing: that is noted, and it clears itself when the reader ends."""
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=0")                      # a reader that holds an old snapshot must not make a write wait
            try:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                try:
                    busy, log, done = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                except sqlite3.Error:
                    busy, log, done = 1, -1, -1
            finally:
                self._conn.execute("PRAGMA busy_timeout=5000")
        if busy:
            if self.checkpoint_blocked_since is None:
                self.checkpoint_blocked_since = self.clock()
                self.counters["checkpoints_blocked"] += 1
        else:
            self.checkpoint_blocked_since = None
        return busy, log, done

    def _housekeeping(self) -> None:
        """Over its target the WAL is checkpointed, at most every two seconds, so a blocked checkpoint cannot slow every write."""
        now = time.monotonic()
        if now - self._last_housekeeping < 2.0:
            return
        if os.path.exists(self.path + "-wal") and os.path.getsize(self.path + "-wal") >= self.config.storage.wal_target_mb * 1024 * 1024:
            self._last_housekeeping = now
            self.checkpoint()

    def unfinished_bytes(self, summary: bool | None = None) -> int:
        where = "" if summary is None else (" AND kind='summary'" if summary else " AND kind!='summary'")
        row = self.read_one(f"SELECT COALESCE(SUM(payload_bytes),0) FROM messages WHERE state IN ('pending','sending','retry_wait') AND payload IS NOT NULL{where}")
        return int(row[0])

    # -- meta --------------------------------------------------------------------------------------------------------------------
    def get_meta(self, name: str, default=None):
        row = self.read_one("SELECT value FROM meta WHERE name=?", (name,))
        return json.loads(row[0]) if row else default

    def set_meta(self, name: str, value, conn=None) -> None:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if conn is not None:
            conn.execute("INSERT INTO meta(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, text))
        else:
            with self.txn() as c:
                self.set_meta(name, value, c)

    def _meta(self, conn, name: str, default=None):
        row = conn.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    # -- states ------------------------------------------------------------------------------------------------------------------
    def _move(self, conn, row, new: str, trigger: str, now: float, **fields) -> None:
        """The one place a message changes state: the table must allow the move, and the row must still be in the state it was read in."""
        states.check(row["state"], new, trigger)
        sets = ["state=?", "updated_at=?"] + [f"{name}=?" for name in fields]
        cursor = conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=? AND state=?", [new, now, *fields.values(), row["id"], row["state"]])
        if cursor.rowcount != 1:
            raise IllegalTransition("the message changed state while it was being moved")

    def get(self, msg_id: int):
        return self.read_one("SELECT * FROM messages WHERE id=?", (msg_id,))

    def by_key(self, key: str):
        return self.read_one("SELECT * FROM messages WHERE key=?", (key,))

    # -- usage and budgets -------------------------------------------------------------------------------------------------------
    def _used(self, conn, column: str, period: str) -> dict:
        rows = conn.execute(f"SELECT klass, COUNT(*) FROM messages WHERE charge_state='charged' AND {column}=? GROUP BY klass", (period,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def _reserved(self, conn) -> dict:
        rows = conn.execute("SELECT klass, COUNT(*) FROM messages WHERE charge_state='reserved' AND state IN ('pending','retry_wait') GROUP BY klass").fetchall()
        return {r[0]: r[1] for r in rows}

    def usage(self, now: float) -> dict:
        """What the notifier itself has used of the UTC day and month of `now`, by class."""
        with self._lock:
            return {"day": self._used(self._conn, "charge_day", utc_day(now)), "month": self._used(self._conn, "charge_month", utc_month(now))}

    def _fits(self, conn, klass: str, now: float, extra: dict | None = None) -> bool:
        extra = extra or {}
        for column, period, limits in (("charge_day", utc_day(now), self.day), ("charge_month", utc_month(now), self.month)):
            used = self._used(conn, column, period)
            merged = {k: used.get(k, 0) + extra.get(k, 0) for k in (budget.SUMMARY, budget.REGULAR, budget.NOTICE)}
            if not budget.allows(klass, merged, limits):
                return False
        return True

    def _burst_ok(self, conn, klass: str, now: float) -> bool:
        if klass != budget.REGULAR:
            return True
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE klass='regular' AND charge_state='charged' AND first_attempt_at>=?", (now - 3600,)).fetchone()[0]
        return count < self.config.budgets.hourly_burst

    # -- persist -----------------------------------------------------------------------------------------------------------------
    def persist(self, *, kind: str, key: str, body: bytes | None, now: float, context: dict | None = None, deadline: float | None = None, slot: str | None = None, due_at: float | None = None,
                low_priority: bool = False, resent_of: int | None = None, skip: bool = False, replacing: list | None = None, meta_set: dict | None = None) -> PersistResult:
        """Store a message, once. The payload, its hash, the idempotency key and the reservation of its budget are committed together, or none of them are.

        A repeat of the same key returns the stored row and changes nothing, so a webhook delivered twice, or two at the same moment, make one message. Raises StorageRefused for a low priority
        write that storage cannot take, PayloadTooLarge for a body over its limit (nothing is stored), and StorageUnavailable when the ledger cannot be written.

        `replacing` is for a digest: the ids of the held messages it stands in for. They are ended in the same transaction as the digest is stored, so either both happen or neither does (a crash or a
        write failure between them cannot leave held alerts gone with no digest). If the digest cannot be stored as a pending message (no budget), nothing is written and the held ones stay."""
        s = self.config.storage
        klass = budget.class_of(kind)
        existing = self.by_key(key)
        if existing is not None:
            return PersistResult(existing["id"], False, existing["state"])
        if body is not None:
            try:
                payload.check_size(kind, body)
            except PayloadTooLarge:
                self.counters["payload_too_large"] += 1
                raise
        if low_priority:
            status = self.storage_status()
            if status.mode in ("low_priority_refused", "full"):
                self.counters["low_priority_refused"] += 1
                raise StorageRefused(status.reasons[0] if status.reasons else status.mode)
        self._housekeeping()
        context_text = None
        if context is not None:
            context_text = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if len(context_text.encode("utf-8")) > MAX_CONTEXT_BYTES:
                context_text = json.dumps({"truncated": True})
        with self.txn() as conn:
            row = conn.execute("SELECT id, state FROM messages WHERE key=?", (key,)).fetchone()
            if row is not None:
                return PersistResult(row["id"], False, row["state"])
            new_state, trigger, store_body = states.PENDING, "persist", body
            if skip:
                new_state, trigger, store_body = states.SKIPPED, "persist_outside_catch_up", None
            elif body is None:
                raise ValueError("a message that is to be sent needs a body")
            else:
                if klass != budget.SUMMARY:
                    reserved = self._reserved(conn)
                    if not self._fits(conn, klass, now, extra=reserved):
                        new_state, trigger, store_body = states.BUDGET_SUPPRESSED, "persist_no_budget", None
                if store_body is not None:
                    mine = len(store_body)
                    total_unfinished = conn.execute("SELECT COALESCE(SUM(payload_bytes),0) FROM messages WHERE state IN ('pending','sending','retry_wait') AND payload IS NOT NULL").fetchone()[0]
                    summary_unfinished = conn.execute("SELECT COALESCE(SUM(payload_bytes),0) FROM messages WHERE state IN ('pending','sending','retry_wait') AND payload IS NOT NULL AND kind='summary'").fetchone()[0]
                    cap, reserve = s.unfinished_payload_mb * 1024 * 1024, s.summary_reserve_mb * 1024 * 1024
                    if kind == "summary":
                        if summary_unfinished + mine > reserve or total_unfinished + mine > cap:
                            raise StorageRefused("unfinished_bytes_summary_reserve")
                    elif (total_unfinished - summary_unfinished) + mine > cap - reserve:
                        raise StorageRefused("unfinished_bytes")
                    if klass != budget.SUMMARY and new_state == states.PENDING:
                        self._make_room_in_queue(conn, now)
            if replacing is not None and new_state != states.PENDING:
                return PersistResult(0, False, new_state)
            states.check(states.NEW, new_state, trigger)
            sha = payload.sha256(body) if body is not None else payload.sha256(b"")
            cursor = conn.execute(
                "INSERT INTO messages(key, kind, klass, state, created_at, updated_at, due_at, deadline, slot, payload, payload_sha256, payload_bytes, idempotency_key, context, charge_state, resent_of)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, kind, klass, new_state, now, now, due_at, deadline, slot, store_body, sha, len(body) if body is not None else 0, payload.idempotency_key(key), context_text,
                 "reserved" if new_state == states.PENDING else "none", resent_of))
            if replacing:
                ended = 0
                for msg_id in replacing:
                    held = conn.execute("SELECT * FROM messages WHERE id=? AND state IN ('pending','retry_wait') AND kind IN ('alert','recovery')", (msg_id,)).fetchone()
                    if held is not None:
                        self._end_waiting(conn, held, "replaced_by_digest", now)
                        ended += 1
                    conn.execute("UPDATE messages SET digest_owed=0 WHERE id=?", (msg_id,))             # the digest now carries it: the duty is discharged in the same commit
                if ended:
                    self._count_event(conn, "expired_digest", ended)
            for name, value in (meta_set or {}).items():
                self.set_meta(name, value, conn)
            return PersistResult(cursor.lastrowid, True, new_state)

    def _make_room_in_queue(self, conn, now: float) -> None:
        """The held queue is bounded. At its limit the oldest alert and recovery rows are expired, and counted."""
        limit = self.config.breaker.queue_max_rows
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE klass!='summary' AND state IN ('pending','retry_wait')").fetchone()[0]
        excess = count - limit + 1
        if excess <= 0:
            return
        for row in conn.execute("SELECT * FROM messages WHERE klass!='summary' AND kind IN ('alert','recovery','digest') AND state IN ('pending','retry_wait') ORDER BY created_at, id LIMIT ?", (excess,)).fetchall():
            self._end_waiting(conn, row, "queue_overflow", now)
            self._count_event(conn, "expired_queue_overflow")

    def _count_event(self, conn, name: str, amount: int = 1) -> None:
        counts = self._meta(conn, "events", {})
        counts[name] = counts.get(name, 0) + amount
        self.set_meta("events", counts, conn)

    def events(self) -> dict:
        return self.get_meta("events", {})

    # -- attempts ----------------------------------------------------------------------------------------------------------------
    def begin_attempt(self, msg_id: int, now: float, probe: bool = False) -> Attempt | Blocked:
        """Record that an attempt is starting, and charge the budget for the first one. Nothing is sent until this has committed.

        Returns Blocked(reason) when the message may not go now: it stays where it is, or, for a message with no budget by its deadline, becomes budget-suppressed."""
        s = self.config.budgets
        self._housekeeping()
        with self.txn() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            if row is None:
                raise IllegalTransition("no such message")
            if row["state"] not in (states.PENDING, states.RETRY_WAIT):
                raise IllegalTransition(f"a message in {row['state']} cannot start an attempt")
            if row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
                return Blocked("not_due")
            if row["first_attempt_at"] is not None and now >= row["first_attempt_at"] + WINDOW_SECONDS - WINDOW_MARGIN:
                self._settle_window(conn, row, now)
                return Blocked("window_passed")
            block_until = self._meta(conn, "quota_block_until", 0)
            if block_until > now:
                return Blocked("quota_block")
            observed = self._meta(conn, "observed", {})
            if observed.get("day") == utc_day(now) and observed.get("daily_used", 0) >= s.account_headroom_daily:
                return Blocked("account_headroom_daily")
            if observed.get("month") == utc_month(now) and observed.get("monthly_used", 0) >= s.account_headroom_monthly:
                return Blocked("account_headroom_monthly")
            if row["payload"] is None:
                raise StorageUnavailable("the message has no stored payload")
            first = row["state"] == states.PENDING
            fields = {"attempts": row["attempts"] + 1, "last_attempt_at": now, "next_attempt_at": None}
            if row["charge_state"] == "reserved":
                if not (self._fits(conn, row["klass"], now) and self._burst_ok(conn, row["klass"], now)):
                    if row["deadline"] is not None and now >= row["deadline"]:
                        self._move(conn, row, states.BUDGET_SUPPRESSED, "no_budget_by_deadline", now, payload=None, charge_state="none")
                        return Blocked("budget_suppressed")
                    return Blocked("budget")
                fields.update(charge_state="charged", charge_day=utc_day(now), charge_month=utc_month(now))
            if row["first_attempt_at"] is None:
                fields["first_attempt_at"] = now                          # the window belongs to the key: a manual resend of the same key never restarts it
            if probe:
                breaker = self._meta(conn, "breaker", {"state": "closed"})
                breaker.update(state="half_open", probe_id=msg_id)
                self.set_meta("breaker", breaker, conn)
            self._move(conn, row, states.SENDING, "first_attempt" if first else "next_attempt", now, **fields)
            return Attempt(row["id"], row["kind"], bytes(row["payload"]), row["payload_sha256"], row["idempotency_key"], row["attempts"] + 1, probe)

    def _settle_window(self, conn, row, now: float) -> None:
        """A waiting message whose idempotency window has run out: settled by what its last outcome was."""
        if row["last_outcome"] == "definitive" and not row["maybe_accepted"]:
            self._move(conn, row, states.FAILED, "window_passed_definitive", now, charge_state=self._refund(row))
        else:
            self._move(conn, row, states.UNCONFIRMED, "window_passed_unknown", now, payload=None)

    @staticmethod
    def _refund(row) -> str:
        """The charge goes back only when Resend is known not to have accepted the message. While an earlier attempt may have been accepted, the charge stays."""
        if row["maybe_accepted"]:
            return row["charge_state"]
        return "refunded" if row["charge_state"] == "charged" else row["charge_state"]

    def _retry_delay(self, attempts: int, retry_after: float | None) -> float:
        r = self.config.retry
        base = r.backoff_seconds[min(attempts - 1, len(r.backoff_seconds) - 1)]
        jittered = base * (1 + r.jitter * (self.rng.random() * 2 - 1))
        return max(jittered, retry_after or 0)

    def finish_attempt(self, msg_id: int, outcome: Outcome, now: float) -> Finished:
        """Record how an attempt ended, in one transaction with everything it changes (the state, the charge, the breaker, the observed quota)."""
        r = self.config.retry
        with self.txn() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            if row is None or row["state"] != states.SENDING:
                raise IllegalTransition("only a message that is sending can finish an attempt")
            self._observe(conn, outcome, now)
            common = {"last_status": outcome.status, "last_error": outcome.error_name}
            event = None
            if outcome.kind == ACCEPTED:
                self._move(conn, row, states.ACCEPTED, "accepted", now, resend_id=outcome.resend_id, accepted_at=now, last_outcome="accepted", next_attempt_at=None, **common)
                return Finished(states.ACCEPTED, self._breaker_success(conn))
            # An answer that says "not accepted" speaks only for this attempt. If an earlier attempt ended unknown, that one may have been accepted, and nothing said now can show otherwise
            # (a 401 or a quota 429 is answered before the key is looked at). So the doubt, and the charge, stay until the message is accepted or its window passes.
            doubt = bool(row["maybe_accepted"])
            if outcome.kind in (QUOTA_DAILY, QUOTA_MONTHLY):
                until = next_utc_midnight(now) if outcome.kind == QUOTA_DAILY else next_utc_month(now)
                self.set_meta("quota_block_until", until, conn)
                kept = {} if doubt else {"charge_state": "reserved", "charge_day": None, "charge_month": None}
                self._move(conn, row, states.RETRY_WAIT, "quota_block", now, attempts=max(row["attempts"] - 1, 0), next_attempt_at=until, last_outcome="unknown" if doubt else "definitive", **kept, **common)
                return Finished(states.RETRY_WAIT)
            if outcome.kind in (CHANGED_PAYLOAD, PERMANENT):
                trigger = "changed_payload" if outcome.kind == CHANGED_PAYLOAD else "permanent_refusal"
                if outcome.kind == CHANGED_PAYLOAD:
                    self._count_event(conn, "changed_payload")
                if doubt:
                    self._move(conn, row, states.UNCONFIRMED, "refused_after_unknown", now, last_outcome="unknown", next_attempt_at=None, **common)      # the bytes stay until the window passes
                    return Finished(states.UNCONFIRMED, self._breaker_failure(conn, now) if outcome.kind == PERMANENT else None)
                self._move(conn, row, states.FAILED, trigger, now, last_outcome="definitive", next_attempt_at=None, charge_state=self._refund(row), **common)
                return Finished(states.FAILED, self._breaker_failure(conn, now) if outcome.kind == PERMANENT else None)
            # a retriable failure: rate limit, a 409 that says another request is in progress, a 5xx, a timeout or a reset connection
            last = "definitive" if outcome.definitive and not doubt else "unknown"
            if not outcome.definitive:
                common["maybe_accepted"] = 1
            if outcome.kind in FAILURE_KINDS:
                event = self._breaker_failure(conn, now)
            attempts = row["attempts"]
            delay = self._retry_delay(attempts, outcome.retry_after)
            first = row["first_attempt_at"] or now
            if attempts >= r.max_attempts or now + delay >= first + WINDOW_SECONDS - WINDOW_MARGIN:
                if last == "definitive":
                    self._move(conn, row, states.FAILED, "attempts_used_definitive", now, last_outcome=last, next_attempt_at=None, charge_state=self._refund(row), **common)
                    return Finished(states.FAILED, event)
                self._move(conn, row, states.UNCONFIRMED, "attempts_used_unknown", now, last_outcome=last, next_attempt_at=None, **common)      # the bytes stay until the window passes, so a manual resend can use them
                return Finished(states.UNCONFIRMED, event)
            self._move(conn, row, states.RETRY_WAIT, "retriable_failure", now, last_outcome=last, next_attempt_at=now + delay, **common)
            return Finished(states.RETRY_WAIT, event)

    def _observe(self, conn, outcome: Outcome, now: float) -> None:
        """What Resend said about the account's use, kept apart from the notifier's own use, with the period it belongs to."""
        if outcome.daily_used is None and outcome.monthly_used is None:
            return
        observed = self._meta(conn, "observed", {})
        if outcome.daily_used is not None:
            observed.update(day=utc_day(now), daily_used=outcome.daily_used)
        if outcome.monthly_used is not None:
            observed.update(month=utc_month(now), monthly_used=outcome.monthly_used)
        observed["at"] = now
        self.set_meta("observed", observed, conn)

    # -- the circuit breaker ---------------------------------------------------------------------------------------------------
    def breaker(self) -> dict:
        return self.get_meta("breaker", {"state": "closed", "failures": 0})

    def _breaker_failure(self, conn, now: float) -> str | None:
        b = self.config.breaker
        state = self._meta(conn, "breaker", {"state": "closed", "failures": 0})
        event = None
        if state["state"] == "half_open":
            cooldown = min(state.get("cooldown", b.cooldown_initial_minutes * 60) * 2, b.cooldown_max_hours * 3600)
            state = {"state": "open", "failures": state.get("failures", 0) + 1, "opened_at": now, "cooldown": cooldown, "probe_id": None}
            event = "reopened"
        elif state["state"] == "closed":
            state["failures"] = state.get("failures", 0) + 1
            if state["failures"] >= b.failures_to_open:
                state = {"state": "open", "failures": state["failures"], "opened_at": now, "cooldown": b.cooldown_initial_minutes * 60, "probe_id": None}
                event = "opened"
        self.set_meta("breaker", state, conn)
        return event

    def _breaker_success(self, conn) -> str | None:
        state = self._meta(conn, "breaker", {"state": "closed", "failures": 0})
        was_open = state["state"] in ("open", "half_open")
        self.set_meta("breaker", {"state": "closed", "failures": 0}, conn)
        if was_open:
            self.set_meta("digest_due", {"episode": int(state.get("opened_at") or 0)}, conn)           # in the same transaction as the close, so a crash cannot lose the duty to send a digest
        return "closed" if was_open else None

    def breaker_gate(self, kind: str, now: float) -> str:
        """allow, hold, or probe. While the breaker is open, alert and recovery mail is held, and after the cool-down one message at a time goes out as a probe."""
        state = self.breaker()
        if state["state"] == "closed":
            return "allow"
        if state["state"] == "half_open":
            return "hold"                                                                  # a probe is in flight
        if now >= state.get("opened_at", 0) + state.get("cooldown", 0) and kind in PROBE_KINDS:
            return "probe"
        return "hold"

    # -- restart -----------------------------------------------------------------------------------------------------------------
    def recover_after_restart(self, now: float) -> dict:
        """What a restart finds. A message left in `sending` is an attempt whose outcome is unknown, and it moves like any other unknown outcome: to retry_wait if attempts remain and the idempotency
        window is open (a repeat with the same key and bytes then settles it without a duplicate), else to unconfirmed. A half-open breaker goes back to open."""
        r = self.config.retry
        report = {"retry_wait": 0, "unconfirmed": 0, "breaker_reopened": 0}
        with self.txn() as conn:
            for row in conn.execute("SELECT * FROM messages WHERE state='sending'").fetchall():
                first = row["first_attempt_at"] or now
                if row["attempts"] < r.max_attempts and now < first + WINDOW_SECONDS - WINDOW_MARGIN:
                    self._move(conn, row, states.RETRY_WAIT, "restart_found_sending", now, last_outcome="unknown", next_attempt_at=now, maybe_accepted=1)
                    report["retry_wait"] += 1
                else:
                    self._move(conn, row, states.UNCONFIRMED, "restart_window_passed", now, last_outcome="unknown", next_attempt_at=None, payload=None, maybe_accepted=1)
                    report["unconfirmed"] += 1
            state = self._meta(conn, "breaker", {"state": "closed", "failures": 0})
            if state["state"] == "half_open":
                state.update(state="open", probe_id=None)
                self.set_meta("breaker", state, conn)
                report["breaker_reopened"] = 1
        return report

    # -- what is due -----------------------------------------------------------------------------------------------------------
    def due(self, now: float, limit: int = 50) -> list:
        rows = self.read("SELECT * FROM messages WHERE state IN ('pending','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at, id", (now,))
        rows.sort(key=lambda r: (PRIORITY.get(r["kind"], 9), r["created_at"], r["id"]))
        return rows[:limit]

    # -- maintenance ---------------------------------------------------------------------------------------------------------------
    def expire_overdue(self, now: float, replaced_by_digest: bool = False) -> int:
        """Alerts, recoveries and notices that waited past their deadline are expired. A summary is never expired here."""
        trigger = "replaced_by_digest" if replaced_by_digest else "deadline_passed"
        count = 0
        with self.txn() as conn:
            for row in conn.execute("SELECT * FROM messages WHERE state IN ('pending','retry_wait') AND kind!='summary' AND deadline IS NOT NULL AND deadline<=?", (now,)).fetchall():
                self._end_waiting(conn, row, trigger, now)
                count += 1
            if count:
                self._count_event(conn, "expired_deadline", count)
        return count

    def _end_waiting(self, conn, row, trigger: str, now: float) -> None:
        """A waiting message that will not be sent (past its deadline, pushed out of the queue, replaced by a digest). If an earlier attempt may have been accepted, it is not "expired", which would say
        it never went out: it becomes unconfirmed, with its bytes and charge, until the window passes."""
        owed = {}
        if row["kind"] in ("alert", "recovery") and trigger in ("deadline_passed", "queue_overflow") and self._owes_digest(conn):
            owed = {"digest_owed": 1}                                  # the mail is stale and is not sent, but the digest still owes the reader what it said
        if row["maybe_accepted"]:
            self._move(conn, row, states.UNCONFIRMED, trigger + "_unknown", now, last_outcome="unknown", next_attempt_at=None, **owed)
        else:
            self._move(conn, row, states.EXPIRED, trigger, now, payload=None, charge_state="none" if row["charge_state"] == "reserved" else row["charge_state"], **owed)

    def _owes_digest(self, conn) -> bool:
        """True during an outage (the breaker is not closed) and from the moment it closes until its digest is stored: mail that goes stale in that time is owed to the digest."""
        return self._meta(conn, "breaker", {"state": "closed"}).get("state") != "closed" or self._meta(conn, "digest_due") is not None

    def held_for_digest(self) -> list:
        """The alert and recovery mail waiting now, oldest first, with what a digest says about each. Nothing is changed."""
        rows = self.read("SELECT id, kind, created_at, context FROM messages WHERE kind IN ('alert','recovery') AND (state IN ('pending','retry_wait') OR digest_owed=1) ORDER BY created_at, id")
        return [{"id": r["id"], "kind": r["kind"], "created_at": r["created_at"], "context": json.loads(r["context"]) if r["context"] else {}} for r in rows]

    def bound_digest_owed(self, now: float) -> int:
        """Incidents owed to a digest are kept for DIGEST_OWED_DAYS and at most queue_max_rows of them (the newest). The rest are let go and counted, and the digest says how many."""
        limit = self.config.breaker.queue_max_rows
        with self.txn() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM messages WHERE digest_owed=1 ORDER BY created_at DESC, id DESC").fetchall()]
            old = {r[0] for r in conn.execute("SELECT id FROM messages WHERE digest_owed=1 AND updated_at<=?", (now - DIGEST_OWED_DAYS * 86400,)).fetchall()}
            drop = [i for n, i in enumerate(ids) if n >= limit or i in old]
            for msg_id in drop:
                conn.execute("UPDATE messages SET digest_owed=0 WHERE id=?", (msg_id,))
            if drop:
                self._count_event(conn, "digest_owed_dropped", len(drop))
                self.set_meta("digest_dropped", (self._meta(conn, "digest_dropped", 0) or 0) + len(drop), conn)
        return len(drop)

    def settle_windows(self, now: float) -> int:
        count = 0
        with self.txn() as conn:
            for row in conn.execute("SELECT * FROM messages WHERE state IN ('pending','retry_wait') AND first_attempt_at IS NOT NULL AND first_attempt_at + ? - ? <= ?", (WINDOW_SECONDS, WINDOW_MARGIN, now)).fetchall():
                self._settle_window(conn, row, now)
                count += 1
            for row in conn.execute("SELECT * FROM messages WHERE state='sending' AND first_attempt_at IS NOT NULL AND first_attempt_at + ? <= ?", (WINDOW_SECONDS, now)).fetchall():
                self._move(conn, row, states.UNCONFIRMED, "window_passed_unknown", now, payload=None, last_outcome="unknown")
                count += 1
            conn.execute("UPDATE messages SET payload=NULL WHERE state='unconfirmed' AND payload IS NOT NULL AND first_attempt_at IS NOT NULL AND first_attempt_at + ? <= ?", (WINDOW_SECONDS, now))
        return count

    def bound_protected(self, now: float) -> int:
        """Unreviewed failed and unconfirmed messages are kept, with a bound: past it, or past their age, the oldest are expired and counted, so they cannot grow without limit."""
        s = self.config.storage
        count = 0
        with self.txn() as conn:
            for row in conn.execute("SELECT * FROM messages WHERE state IN ('failed','unconfirmed') AND updated_at<=?", (now - s.protected_days * 86400,)).fetchall():
                self._move(conn, row, states.EXPIRED, "protected_age", now, payload=None)
                count += 1
            total = conn.execute("SELECT COUNT(*) FROM messages WHERE state IN ('failed','unconfirmed')").fetchone()[0]
            if total > s.protected_max_rows:
                for row in conn.execute("SELECT * FROM messages WHERE state IN ('failed','unconfirmed') ORDER BY updated_at, id LIMIT ?", (total - s.protected_max_rows,)).fetchall():
                    self._move(conn, row, states.EXPIRED, "protected_overflow", now, payload=None)
                    count += 1
            if count:
                self._count_event(conn, "expired_protected", count)
        return count

    def drop_payloads(self, now: float) -> int:
        """Finished payload bytes are dropped after their days, or earlier when they pass their budget, oldest first. The metadata stays."""
        s = self.config.storage
        final = ",".join(f"'{x}'" for x in states.FINAL + states.PROTECTED)          # failed and unconfirmed rows hold payload bytes too, so they are inside the same budget
        dropped = 0
        with self.txn() as conn:
            dropped += conn.execute(f"UPDATE messages SET payload=NULL WHERE state IN ({final}) AND payload IS NOT NULL AND updated_at<=?", (now - s.finished_payload_days * 86400,)).rowcount
            budget_bytes = s.finished_payload_mb * 1024 * 1024
            held = conn.execute(f"SELECT COALESCE(SUM(length(payload)),0) FROM messages WHERE state IN ({final}) AND payload IS NOT NULL").fetchone()[0]
            if held > budget_bytes:
                for row in conn.execute(f"SELECT id, length(payload) AS n FROM messages WHERE state IN ({final}) AND payload IS NOT NULL ORDER BY updated_at, id").fetchall():
                    conn.execute("UPDATE messages SET payload=NULL WHERE id=?", (row["id"],))
                    dropped += 1
                    held -= row["n"]
                    if held <= budget_bytes:
                        break
        return dropped

    def delete_old(self, now: float) -> int:
        s = self.config.storage
        final = ",".join(f"'{x}'" for x in states.FINAL)
        with self.txn() as conn:
            return conn.execute(f"DELETE FROM messages WHERE state IN ({final}) AND digest_owed=0 AND updated_at<=?", (now - s.metadata_days * 86400,)).rowcount

    BANDWIDTH_KEEP_DAYS = 100                                          # a billing month can start anywhere, so more than a month of hours is kept

    def store_bandwidth(self, rows: list) -> int:
        """Store measured hours as (hour, bytes). A measured hour is never changed by a later write. Hours older than the retention are dropped."""
        if not rows:
            return 0
        with self.txn() as conn:
            for hour, size in rows:
                conn.execute("INSERT OR IGNORE INTO bandwidth(hour, bytes, measured) VALUES (?,?,1)", (int(hour), int(size)))
            conn.execute("DELETE FROM bandwidth WHERE hour < ?", (int(max(h for h, _ in rows)) - self.BANDWIDTH_KEEP_DAYS * 24,))
        return len(rows)

    def bandwidth_hours(self, first: int, last: int) -> set:
        """The measured hours in [first, last)."""
        return {r[0] for r in self.read("SELECT hour FROM bandwidth WHERE measured=1 AND hour>=? AND hour<?", (first, last))}

    def prune_for_size(self, now: float) -> int:
        """At the prune threshold, finished rows are deleted oldest first until the file is comfortably below it. Active and unreviewed rows are never touched by size."""
        s = self.config.storage
        cap = s.max_db_mb * 1024 * 1024
        final = ",".join(f"'{x}'" for x in states.FINAL)
        deleted = 0
        if self.storage_status().used_bytes < cap * s.prune_at_percent / 100:
            return 0
        target = cap * (s.prune_at_percent - 10) / 100
        while self.storage_status().used_bytes >= target:
            with self.txn() as conn:
                ids = [r[0] for r in conn.execute(f"SELECT id FROM messages WHERE state IN ({final}) AND digest_owed=0 ORDER BY updated_at, id LIMIT 8").fetchall()]      # small batches, so it stops near the target
                if not ids:
                    break
                conn.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)
                deleted += len(ids)
            with self._lock:
                self._conn.execute("PRAGMA incremental_vacuum")
        self.checkpoint()
        return deleted

    def maintain(self, now: float) -> dict:
        report = {"expired": self.expire_overdue(now), "settled": self.settle_windows(now), "protected": self.bound_protected(now), "payloads": self.drop_payloads(now),
                  "owed": self.bound_digest_owed(now), "deleted": self.delete_old(now), "pruned": self.prune_for_size(now)}
        with self._lock:
            self._conn.execute("PRAGMA incremental_vacuum(64)")
        return report

    # -- review and manual resend --------------------------------------------------------------------------------------------------
    def review(self, msg_id: int, now: float) -> None:
        with self.txn() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            if row is None:
                raise IllegalTransition("no such message")
            self._move(conn, row, states.REVIEWED, "operator_review", now, reviewed_at=now, payload=None)

    def manual_resend(self, msg_id: int, now: float) -> tuple[int, bool]:
        """A failed or unconfirmed message sent again by the operator, after checking. Inside the idempotency window the same bytes and key go again (and the key cannot be accepted twice). After
        it, a new message is made from the stored bytes, with a new key, and a duplicate is possible: the caller must say so. Returns (id, same_key)."""
        row = self.get(msg_id)
        if row is None:
            raise IllegalTransition("no such message")
        within = row["first_attempt_at"] is not None and now < row["first_attempt_at"] + WINDOW_SECONDS - WINDOW_MARGIN and row["payload"] is not None
        if within:
            trigger = "manual_resend" if row["state"] == states.FAILED else "manual_resend_in_window"
            with self.txn() as conn:
                row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
                self._move(conn, row, states.PENDING, trigger, now, attempts=0, next_attempt_at=None, deadline=None, charge_state="reserved" if row["charge_state"] != "charged" else "charged")
            return msg_id, True
        if row["payload"] is None:
            raise IllegalTransition("the stored bytes are gone, so there is nothing to resend")
        new_key = f"{row['key']}/resend/{int(now)}"
        result = self.persist(kind=row["kind"], key=new_key, body=bytes(row["payload"]), now=now, context=json.loads(row["context"]) if row["context"] else None, deadline=None,
                              slot=row["slot"], resent_of=msg_id)
        return result.id, False

    # -- reports -------------------------------------------------------------------------------------------------------------------
    def counts(self) -> dict:
        rows = self.read("SELECT kind, state, COUNT(*) FROM messages GROUP BY kind, state")
        return {(r[0], r[1]): r[2] for r in rows}

    def overdue_report(self, now: float, grace_seconds: float) -> dict:
        """What the alerts watch: a scheduled summary overdue (zero when nothing is due), the largest overshoot of a message past its deadline, and how many failed or unconfirmed are unreviewed."""
        row = self.read_one("SELECT MAX(? - due_at - ?) FROM messages WHERE kind='summary' AND due_at IS NOT NULL AND state IN ('pending','retry_wait','sending','failed','unconfirmed') AND due_at + ? <= ?",
                            (now, grace_seconds, grace_seconds, now))
        overdue = max(row[0] or 0, 0)
        row = self.read_one("SELECT MAX(? - deadline) FROM messages WHERE state IN ('pending','retry_wait','sending') AND deadline IS NOT NULL AND deadline<=? AND kind NOT IN ('email_path','host')", (now, now))
        overshoot = max(row[0] or 0, 0)
        failed = self.read_one("SELECT COUNT(*) FROM messages WHERE state IN ('failed','unconfirmed') AND kind NOT IN ('email_path','host')")[0]
        return {"summary_overdue_seconds": overdue, "oldest_overshoot_seconds": overshoot, "failed_or_unconfirmed": failed}
