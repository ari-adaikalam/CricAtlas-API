"""Incremental state for the daily refresh: reuse what has not changed, rebuild what has, and say which.

The build is a chain of per-match steps followed by global steps. Three per-match results are kept between runs, each under a key made from the match's own content, so a new, corrected or removed match is
found by its content and not by a date or a file name:

  ingest      the match's ball rows and the rows of every table the JSON adds. Key: SHA-256 of the match's three archive members (<id>.csv, <id>_info.csv, <id>.json), its collection, and the hash of the
              code that reads them. Beside each entry the state keeps two digests of its result, the ball rows and the other tables, with the three provenance fields of the facts left out
              (when Cricsheet created the record, its revision and the version of its data format).
  rows        the match's final lines of ball_by_ball_data.csv (profile columns attached) and its row for matches.csv. Key: the digest of the match's ball rows, the player profile snapshot's hash,
              and the hash of the code.
  scorecards  the stored scorecard text of the match. Key: the two digests of the match's ingest result and the hash of the scorecard code and the code that shapes its rows. A scorecard reads only
              the match's own rows in the ball, match, wicket, fielder, squad, innings and over tables, so a profile change does not touch it (the audit below would catch the day that stopped being true).

The rows and scorecards keys come from what the ingest layer produced and not from the files it read (an early cutoff). When Cricsheet rewrites every file of every match but a match's result is the same,
only the ingest layer runs again for it, and the layers above it are reused. A match whose result did change is rebuilt in all three layers.

Everything else is global and is recomputed from the CSV files every run: the people register and player_external_ids, matches.csv, the JSON tables, players and player_names, every aggregate table
(batting innings, bowling spells, fielding events, involvement, batter against bowler), the checks and the version hash. They depend on more than one match, and recomputing them is a few minutes.

A run that finds nothing to do can end early. When the code, the profile snapshot, the people register and every match's three files are the same as at the last full pass, and the data that pass built is the
data that is live, the preparation is "unchanged" without building or exporting anything. A full pass is still made at least every seven days.

The state is an accelerator and never an authority. An entry is used only when its key equals the key the current input would give, and each run rebuilds a random sample of the entries it reused and compares
(the audit). A difference raises Mismatch, and the caller discards the state and runs the whole build again. The state is one SQLite file, written in transactions, so a killed run leaves whole entries
or none. A change to the hash of the code makes the entries it reads unusable and the next run rebuilds them. A state of the previous format is migrated in place, and its rows and scorecards entries are re-keyed only when
their old key proves they were built from the ingest entry that is kept. Its size is capped. It holds no birth dates:
ingest rows come before profiles are attached, and the final rows carry an age in years only.

Off unless a state folder is given (CRICKET_REFRESH_STATE_DIR), and off when CRICKET_REFRESH_INCREMENTAL is 0. Nothing here changes what a build writes.
"""
import contextlib
import gzip
import hashlib
import json
import os
import pickle
import random
import sqlite3
import sys
import time
from pathlib import Path

ENGINE_VERSION = 2               # in the keys of the rows and scorecards layers, which are built from the ingest result since the early cutoff
INGEST_KEY_VERSION = 1           # the ingest key did not change with it, so every ingest entry kept before stays valid
LEGACY_ENGINE_VERSION = 1        # the version in the rows and scorecards keys before the early cutoff, for the one migration
STATE_FORMAT = 2
MAX_STATE_BYTES = 2 * 1024 ** 3
AUDIT_PER_LAYER = 24
QUICK_MAX_AGE = 7 * 24 * 3600    # a run may end early only while the last full pass is younger than this
PROVENANCE_FIELDS = ("cricsheet_created", "cricsheet_revision", "cricsheet_data_version")      # Cricsheet's bookkeeping of a match record: no scorecard reads them (it reads match_id and balls_per_over)
REFRESH_ROOT = Path(__file__).resolve().parent
API_ROOT = REFRESH_ROOT.parents[1]

INGEST_FILES = [REFRESH_ROOT / name for name in ("cricsheet.py", "cricsheet_json.py", "matches.py", "pipeline.py", "config.py")]
BUILD_FILES = [REFRESH_ROOT / name for name in ("build.py", "profiles.py")]
SCORECARD_FILES = [API_ROOT / "cricket_api" / name for name in ("scorecard.py", "metrics.py", "export.py")]


# The kept results are pickled pandas data, and unpickling can run code. That is safe here only because of where the file lives: the state folder is mode 700 and the file
# mode 600, both owned by the refresh account that runs this code, and the file is never downloaded or copied from anywhere. Cricsheet's data is parsed into frames and
# pickled by this code, and only those blobs are read back. The SHA-256 beside each blob catches corruption, not tampering, because anyone who can edit the file can write a
# new digest. Whoever can edit the file already has this account's powers. If the state ever has to cross a trust boundary, sign the blobs or replace pickle first.


class Mismatch(Exception):
    """A reused entry differs from what the current input gives. The state cannot be trusted: rebuild everything."""


def code_hash(files) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def digest_of(*parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = part if isinstance(part, bytes) else str(part).encode()
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def content_digests(value) -> tuple[str, str]:
    """Two digests of an ingest result, which is the pair (the match's ball rows, the rows of the tables the JSON adds). The first is the ball rows as the build reads them: their text as it
    will be written and the type of every column. The second is every other table with the provenance fields of the facts left out. They are written out as JSON with sorted keys, so
    they depend on the content only and not on how the objects were created or shared."""
    rows, tables = value
    ball_rows = digest_of("rows", rows.to_csv(index=False), ",".join(str(kind) for kind in rows.dtypes))
    cleaned = {name: ([{k: v for k, v in row.items() if k not in PROVENANCE_FIELDS} for row in found] if name == "facts" else found) for name, found in tables.items()}
    other = digest_of("tables", json.dumps(cleaned, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False))
    return ball_rows, other


def tree_hash() -> str:
    """SHA-256 of every Python file the refresh and the API export are made of, by relative path. The early end of a run needs more than the three code hashes of the keys, because
    a change to any of these files can change what a build writes even when every match is the same."""
    digest = hashlib.sha256()
    for root in (REFRESH_ROOT, API_ROOT / "cricket_api"):
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(root.parent)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def environment_fingerprint() -> str:
    """The interpreter and the libraries whose version can change what a build writes."""
    from importlib import metadata

    found = []
    for name in ("pandas", "numpy", "duckdb", "pyarrow"):
        try:
            found.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            found.append(f"{name} none")
    return "python {}.{}.{}; ".format(*sys.version_info[:3]) + "; ".join(found)


def state_folder() -> Path | None:
    folder = os.environ.get("CRICKET_REFRESH_STATE_DIR")
    if not folder or os.environ.get("CRICKET_REFRESH_INCREMENTAL", "1") == "0":
        return None
    return Path(folder)


_current = None


def current():
    return _current


class State:
    """The kept results and the counts of this run. `mode` is "use" (reuse what matches) or "populate" (rebuild everything and keep it, which is the full-rebuild fallback)."""

    def __init__(self, folder: Path, mode: str = "use"):
        if mode not in ("use", "populate"):
            raise ValueError("mode must be use or populate")
        self.folder, self.mode = folder, mode
        self.codes = {"ingest": code_hash(INGEST_FILES), "build": code_hash(BUILD_FILES), "scorecard": code_hash(SCORECARD_FILES)}
        self.code_all = tree_hash()
        self.stats = {layer: {"reused": 0, "rebuilt": 0, "corrupt": 0} for layer in ("ingest", "rows", "scorecards")}
        self.classes = {"new": 0, "corrected": 0, "unchanged": 0, "removed": 0}
        self.reset_reason = None
        self.reused: dict[str, list] = {"ingest": [], "rows": [], "scorecards": []}
        self.ingest_keys: dict[int, str] = {}
        self.contents: dict[int, tuple[str, str]] = {}       # the two digests of each match's ingest result, which the rows and scorecards keys are made from
        self.cutoff = {"ingest_same_content": 0}              # matches whose files changed and whose result did not, so the layers above them were reused
        self.migrated = None                                  # set when a state of the previous format was migrated at open
        self.rekeyed = {"rows": 0, "scorecards": 0}           # entries of the previous format that were re-keyed, and not rebuilt
        self.people_digest = ""
        self.keys_loaded = False          # True once every key was read in one pass (the export stage), so a missing id is not looked up one by one
        self.key_lookups = 0              # single-id reads of the ingest table; stays at zero when the keys are in memory
        self.key_seconds = {"lookups": 0.0, "load": 0.0}     # time spent on those reads and on the one-pass load, for the run's log line
        self.profile_digest = ""
        self._pending = 0
        self.members: dict[str, set[int]] = {}
        self.started = time.time()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        self.path = folder / "state.sqlite"
        self.db = self._open()

    # storage ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _open(self):
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            self._quarantine("the state file could not be read")
            return self._connect()

    def _connect(self):
        if self.path.exists() and self.path.stat().st_size > MAX_STATE_BYTES:
            self._quarantine("the state file was over its size limit")
        fresh = not self.path.exists()
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        if fresh:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        found = db.execute("SELECT v FROM meta WHERE k='format'").fetchone()
        legacy = bool(found) and found[0] == f"{STATE_FORMAT - 1}"
        if found and found[0] != f"{STATE_FORMAT}" and not legacy:
            db.close()
            self._quarantine(f"the state format {found[0]} is not {STATE_FORMAT}")
            return self._connect()
        db.execute("CREATE TABLE IF NOT EXISTS ingest (archive TEXT, match_id INTEGER, key TEXT, digest TEXT, blob BLOB, PRIMARY KEY (archive, match_id))")
        db.execute("CREATE TABLE IF NOT EXISTS content (archive TEXT, match_id INTEGER, rows_digest TEXT, tables_digest TEXT, PRIMARY KEY (archive, match_id))")
        db.execute("CREATE TABLE IF NOT EXISTS rows (match_id INTEGER PRIMARY KEY, key TEXT, digest TEXT, blob BLOB)")
        db.execute("CREATE TABLE IF NOT EXISTS scorecards (match_id INTEGER PRIMARY KEY, key TEXT, has_text INTEGER, text TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS members (match_id INTEGER PRIMARY KEY, archive TEXT)")
        if legacy:
            try:
                self._migrate(db)
            except Exception as error:      # a state that cannot be migrated is not worth stopping a run for: it is set aside and rebuilt, as a damaged one is
                if db.in_transaction:
                    db.execute("ROLLBACK")
                db.close()
                self._quarantine(f"the state of format {STATE_FORMAT - 1} could not be migrated ({type(error).__name__})")
                return self._connect()
        else:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('format', ?)", (str(STATE_FORMAT),))
        db.execute("PRAGMA integrity_check(1)").fetchone()
        os.chmod(self.path, 0o600)
        return db

    def _migrate(self, db):
        """Format 1 to 2: give every ingest entry its two content digests, in one transaction, so a killed migration leaves the state as it was. The rows and scorecards keys are
        re-keyed later, by `rekey_legacy`, because proving an old key needs the profile snapshot's hash, which is not known here."""
        began = time.time()
        db.execute("BEGIN IMMEDIATE")
        done = skipped = 0
        for archive, match_id in db.execute("SELECT archive, match_id FROM ingest ORDER BY archive, match_id").fetchall():
            digest, blob = db.execute("SELECT digest, blob FROM ingest WHERE archive=? AND match_id=?", (archive, match_id)).fetchone()
            if hashlib.sha256(blob).hexdigest() != digest:
                skipped += 1            # a damaged entry gets no digests, so it is not reused and the ingest builds it again
                continue
            value = pickle.loads(gzip.decompress(blob))      # trusted local state, see the note above class Mismatch
            db.execute("INSERT OR REPLACE INTO content VALUES (?, ?, ?, ?)", (archive, match_id, *content_digests(value)))
            done += 1
        db.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_rekey', 'pending')")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('format', ?)", (str(STATE_FORMAT),))
        db.execute("COMMIT")
        self.migrated = {"entries": done, "damaged": skipped, "seconds": round(time.time() - began, 1)}

    def _quarantine(self, reason: str):
        self.reset_reason = reason
        for suffix in ("", "-journal"):
            target = Path(str(self.path) + suffix)
            if target.exists():
                target.replace(self.folder / ("state.sqlite.bad" + suffix))

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def _write(self, sql: str, args=()):
        """One write inside a transaction that is committed every few hundred writes, so a killed run keeps its finished entries and never half of one."""
        self._write_all([(sql, args)])

    def _write_all(self, statements):
        """Several statements that belong to one entry (an ingest entry and its digests). They are counted as one write, so a commit never falls between them."""
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        for sql, args in statements:
            self.db.execute(sql, args)
        self._pending += 1
        if self._pending >= 400:
            self.flush()

    def flush(self):
        if self.db.in_transaction:
            self.db.execute("COMMIT")
        self._pending = 0

    # layer 1: ingest ----------------------------------------------------------------------------------------------------------------------------------------------------------------

    def ingest_key(self, source, match_id: int, archive, json_archive) -> str:
        members = [archive.read(f"{match_id}.csv"), archive.read(f"{match_id}_info.csv"), json_archive.read(f"{match_id}.json")]
        return digest_of(INGEST_KEY_VERSION, self.codes["ingest"], source.archive, source.format, source.type, *[hashlib.sha256(m).hexdigest() for m in members])

    def ingest(self, source, match_id: int, archive, json_archive, build):
        key = self.ingest_key(source, match_id, archive, json_archive)
        self.ingest_keys[match_id] = key
        row = self.db.execute("SELECT i.key, i.digest, i.blob, c.rows_digest, c.tables_digest FROM ingest i LEFT JOIN content c ON c.archive = i.archive AND c.match_id = i.match_id "
                              "WHERE i.archive=? AND i.match_id=?", (source.archive, match_id)).fetchone()
        if self.mode == "use" and row and row[0] == key and row[3] is not None:
            if hashlib.sha256(row[2]).hexdigest() == row[1]:
                try:
                    value = pickle.loads(gzip.decompress(row[2]))      # trusted local state, see the note above class Mismatch
                    self.contents[match_id] = (row[3], row[4])
                    self.stats["ingest"]["reused"] += 1
                    self.classes["unchanged"] += 1
                    self.reused["ingest"].append((source, match_id))
                    return value
                except Exception:
                    pass
            self.stats["ingest"]["corrupt"] += 1
        self.classes["corrected" if row and row[0] != key else "new"] += 1
        value = build()
        content = content_digests(value)
        if row and row[0] != key and row[3] is not None and (row[3], row[4]) == content:
            self.cutoff["ingest_same_content"] += 1       # the files changed and the result did not: the rows and scorecards entries of this match stay valid
        blob = gzip.compress(pickle.dumps(value, protocol=4), compresslevel=3)
        self._write_all([("INSERT OR REPLACE INTO ingest VALUES (?, ?, ?, ?, ?)", (source.archive, match_id, key, hashlib.sha256(blob).hexdigest(), blob)),
                         ("INSERT OR REPLACE INTO content VALUES (?, ?, ?, ?)", (source.archive, match_id, *content))])
        self.contents[match_id] = content
        self.stats["ingest"]["rebuilt"] += 1
        return value

    def audit_ingest(self, rebuild):
        """Rebuild a random sample of the reused matches and compare. `rebuild(source, match_id)` gives the same pair the build gives."""
        for source, match_id in self._sample("ingest"):
            row = self.db.execute("SELECT blob FROM ingest WHERE archive=? AND match_id=?", (source.archive, match_id)).fetchone()
            kept_rows, kept_tables = pickle.loads(gzip.decompress(row[0]))      # trusted local state, see the note above class Mismatch
            rows, tables = rebuild(source, match_id)
            if not (kept_rows.equals(rows) and list(kept_rows.dtypes) == list(rows.dtypes) and pickle.dumps(kept_tables, protocol=4) == pickle.dumps(tables, protocol=4)):
                raise Mismatch(f"ingest entry of match {match_id} differs from a fresh build")
            stored = self.db.execute("SELECT rows_digest, tables_digest FROM content WHERE archive=? AND match_id=?", (source.archive, match_id)).fetchone()
            if stored is None or tuple(stored) != content_digests((rows, tables)):
                raise Mismatch(f"the content digests of match {match_id} differ from a fresh build")

    # layer 2: rows ------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _content_of(self, match_id: int) -> tuple[str, str]:
        """The two digests of the match's ingest result: its ball rows and its other tables. Empty strings for a match that has none."""
        found = self.contents.get(match_id)
        if found is None and not self.keys_loaded:
            self.key_lookups += 1
            began = time.perf_counter()
            row = self.db.execute("SELECT rows_digest, tables_digest FROM content WHERE match_id=?", (match_id,)).fetchone()     # keyed by (archive, match_id), so this reads the whole table
            self.key_seconds["lookups"] += time.perf_counter() - began
            found = (row[0], row[1]) if row else None
        return found or ("", "")

    def load_ingest_keys(self):
        """Read every ingest key and every pair of digests in one pass, for the stage that did not ingest. The build stage fills them as it goes and a later archive wins for an id that is in two
        archives, so the archives are walked in membership order (the order they were ingested in) and a later one overwrites an earlier one here too. Needs `members` to be set."""
        began = time.perf_counter()
        stored_keys: dict[str, dict[int, str]] = {}
        for archive, match_id, key in self.db.execute("SELECT archive, match_id, key FROM ingest"):
            stored_keys.setdefault(archive, {})[match_id] = key
        stored_content: dict[str, dict[int, tuple[str, str]]] = {}
        for archive, match_id, rows_digest, tables_digest in self.db.execute("SELECT archive, match_id, rows_digest, tables_digest FROM content"):
            stored_content.setdefault(archive, {})[match_id] = (rows_digest, tables_digest)
        keys: dict[int, str] = {}
        contents: dict[int, tuple[str, str]] = {}
        for archive, ids in self.members.items():
            found_keys, found_content = stored_keys.get(archive, {}), stored_content.get(archive, {})
            for match_id in ids:
                if match_id in found_keys:
                    keys[match_id] = found_keys[match_id]
                if match_id in found_content:
                    contents[match_id] = found_content[match_id]
        self.ingest_keys, self.contents, self.keys_loaded = keys, contents, True
        self.key_seconds["load"] += time.perf_counter() - began

    def rows_key(self, match_id: int) -> str:
        rows_digest, _ = self._content_of(match_id)
        return digest_of(ENGINE_VERSION, self.codes["build"], match_id, rows_digest, self.profile_digest)

    def _legacy_rows_key(self, ingest_key: str) -> str:
        return digest_of(LEGACY_ENGINE_VERSION, self.codes["build"], ingest_key, self.profile_digest)

    def _legacy_scorecard_key(self, ingest_key: str) -> str:
        return digest_of(LEGACY_ENGINE_VERSION, self.codes["scorecard"], self.codes["build"], ingest_key)

    def rekey_legacy(self) -> dict:
        """After a migration from format 1: give the rows and scorecards entries keys of the new kind, but only to those whose old key proves they were built from the ingest entry that is kept
        now, with this profile snapshot and this code. Everything else keeps its old key, which no new key equals, so it is built again. Run it before the ingest of the run, while the ingest
        entries are still the ones the old keys were made from. Needs `profile_digest`. Safe to run again after a kill: an entry already re-keyed is skipped."""
        done = {"rows": 0, "scorecards": 0}
        if self.mode != "use" or self.meta_get("legacy_rekey") != "pending":
            return done
        ingest_keys = {match_id: key for _, match_id, key in self.db.execute("SELECT archive, match_id, key FROM ingest")}
        contents = {match_id: (rows_digest, tables_digest) for _, match_id, rows_digest, tables_digest in self.db.execute("SELECT archive, match_id, rows_digest, tables_digest FROM content")}
        self.contents = {**contents}
        for match_id, key in self.db.execute("SELECT match_id, key FROM rows").fetchall():
            ingest = ingest_keys.get(match_id)
            if ingest is not None and match_id in contents and key == self._legacy_rows_key(ingest):
                self._write("UPDATE rows SET key=? WHERE match_id=?", (self.rows_key(match_id), match_id))
                done["rows"] += 1
        for match_id, key in self.db.execute("SELECT match_id, key FROM scorecards").fetchall():
            ingest = ingest_keys.get(match_id)
            if ingest is not None and match_id in contents and key == self._legacy_scorecard_key(ingest):
                self._write("UPDATE scorecards SET key=? WHERE match_id=?", (self.scorecard_key(match_id), match_id))
                done["scorecards"] += 1
        self.flush()
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_rekey', 'done')")
        self.rekeyed = done
        return done

    def get_rows(self, match_id: int):
        if self.mode != "use":
            return None
        row = self.db.execute("SELECT key, digest, blob FROM rows WHERE match_id=?", (match_id,)).fetchone()
        if row and row[0] == self.rows_key(match_id):
            if hashlib.sha256(row[2]).hexdigest() == row[1]:
                try:
                    value = pickle.loads(gzip.decompress(row[2]))      # trusted local state, see the note above class Mismatch
                    self.stats["rows"]["reused"] += 1
                    self.reused["rows"].append(match_id)
                    return value
                except Exception:
                    pass
            self.stats["rows"]["corrupt"] += 1
        return None

    def put_rows(self, match_id: int, value):
        blob = gzip.compress(pickle.dumps(value, protocol=4), compresslevel=3)
        self._write("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, ?)", (match_id, self.rows_key(match_id), hashlib.sha256(blob).hexdigest(), blob))
        self.stats["rows"]["rebuilt"] += 1

    def audit_rows(self, rebuild_many):
        """`rebuild_many(ids)` gives a fresh output for each id in one pass over the files."""
        ids = self._sample("rows")
        fresh_all = rebuild_many(ids) if ids else {}
        for match_id in ids:
            row = self.db.execute("SELECT blob FROM rows WHERE match_id=?", (match_id,)).fetchone()
            kept = pickle.loads(gzip.decompress(row[0]))      # trusted local state, see the note above class Mismatch
            fresh = fresh_all[match_id]
            if not (kept[1] == fresh[1] and kept[2] == fresh[2] and kept[0].to_csv(index=False) == fresh[0].to_csv(index=False)):     # the match's row is compared as it will be written: its in-memory type of an empty column depends on the read chunk, its text does not
                raise Mismatch(f"row entry of match {match_id} differs from a fresh build")

    # layer 3: scorecards ------------------------------------------------------------------------------------------------------------------------------------------------------------

    def scorecard_key(self, match_id: int) -> str:
        rows_digest, tables_digest = self._content_of(match_id)
        return digest_of(ENGINE_VERSION, self.codes["scorecard"], self.codes["build"], match_id, rows_digest, tables_digest)

    _MISSING = object()

    def get_scorecard(self, match_id: int):
        """The kept text (None for a match with no innings), or State.MISSING."""
        if self.mode != "use":
            return self._MISSING
        row = self.db.execute("SELECT key, has_text, text FROM scorecards WHERE match_id=?", (match_id,)).fetchone()
        if row and row[0] == self.scorecard_key(match_id):
            self.stats["scorecards"]["reused"] += 1
            self.reused["scorecards"].append(match_id)
            return row[2] if row[1] else None
        return self._MISSING

    def put_scorecard(self, match_id: int, text):
        self._write("INSERT OR REPLACE INTO scorecards VALUES (?, ?, ?, ?)", (match_id, self.scorecard_key(match_id), 0 if text is None else 1, text))
        self.stats["scorecards"]["rebuilt"] += 1

    def audit_scorecards(self, rebuild):
        for match_id in self._sample("scorecards"):
            row = self.db.execute("SELECT has_text, text FROM scorecards WHERE match_id=?", (match_id,)).fetchone()
            kept = row[1] if row[0] else None
            if rebuild(match_id) != kept:
                raise Mismatch(f"scorecard entry of match {match_id} differs from a fresh build")

    MISSING = _MISSING

    # shared -------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def _sample(self, layer: str):
        found = self.reused[layer]
        return random.SystemRandom().sample(found, min(AUDIT_PER_LAYER, len(found)))

    def finish(self, members: dict[str, set[int]], fallback: str | None = None, audited: bool = True) -> dict:
        """After a successful build: forget what is no longer in the sources, keep the membership, and write the report."""
        current = {match_id: archive for archive, ids in members.items() for match_id in ids}
        before = {row[0]: row[1] for row in self.db.execute("SELECT match_id, archive FROM members")}
        self.classes["removed"] = len(set(before) - set(current))
        self.flush()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for table in ("ingest", "content", "rows", "scorecards"):
                stale = [row[0] for row in self.db.execute(f"SELECT match_id FROM {table}") if row[0] not in current]
                for offset in range(0, len(stale), 500):
                    chunk = stale[offset:offset + 500]
                    self.db.execute(f"DELETE FROM {table} WHERE match_id IN ({','.join('?' * len(chunk))})", chunk)
            self.db.execute("DELETE FROM members")
            self.db.executemany("INSERT INTO members VALUES (?, ?)", list(current.items()))
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")
        self.db.execute("PRAGMA incremental_vacuum")
        total = len(current)
        report = {"engine": ENGINE_VERSION, "mode": self.mode, "matches": total, "layers": self.stats, "membership": self.classes, "audited": audited,
                  "full_rebuild": self.mode == "populate" or all(s["reused"] == 0 for s in self.stats.values()),
                  "full_rebuild_reason": fallback or self.reset_reason or ("no state to reuse" if all(s["reused"] == 0 for s in self.stats.values()) and self.mode == "use" else None),
                  "cutoff": dict(self.cutoff), "rekeyed": dict(self.rekeyed), "migrated": self.migrated,
                  "state_bytes": self.size(), "state_limit_bytes": MAX_STATE_BYTES, "seconds": round(time.time() - self.started, 1)}
        self._write_report(report)
        return report

    def _write_report(self, report: dict):
        tmp = self.folder / ".last_run.json.tmp"
        tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.folder / "last_run.json")

    # the early end of a run -------------------------------------------------------------------------------------------------------------------------------------------------------

    def meta_get(self, key: str):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def mark_complete(self, built_version: str):
        """Record that a full pass ended well: the data version it built, when, and what it was built from. Called after the audits and the export, never before. Only while this record
        matches the live data and the inputs of the next run may that run end early."""
        self.meta_set("complete", json.dumps({"built_version": built_version, "at_epoch": time.time(), "code_all": self.code_all, "environment": environment_fingerprint(),
                                              "profile_digest": self.profile_digest, "people_digest": self.people_digest, "engine": ENGINE_VERSION}, sort_keys=True))

    def completion(self) -> dict | None:
        found = self.meta_get("complete")
        try:
            value = json.loads(found) if found else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def stored_members(self) -> dict[str, set[int]]:
        found: dict[str, set[int]] = {}
        for match_id, archive in self.db.execute("SELECT match_id, archive FROM members"):
            found.setdefault(archive, set()).add(match_id)
        return found

    def stored_ingest_keys(self) -> dict[tuple[str, int], str]:
        return {(archive, match_id): key for archive, match_id, key in self.db.execute("SELECT archive, match_id, key FROM ingest")}

    def write_quick_report(self, counts: dict, seconds: float) -> dict:
        """The report of a run that ended early, in the shape of a full one: every entry reused, nothing rebuilt."""
        total = sum(len(ids) for ids in self.stored_members().values())
        report = {"engine": ENGINE_VERSION, "mode": self.mode, "matches": total, "layers": {layer: {"reused": total, "rebuilt": 0, "corrupt": 0} for layer in ("ingest", "rows", "scorecards")},
                  "membership": {"new": 0, "corrected": 0, "unchanged": total, "removed": 0}, "audited": False, "full_rebuild": False, "full_rebuild_reason": None, "quick_check": counts,
                  "cutoff": dict(self.cutoff), "rekeyed": dict(self.rekeyed), "migrated": self.migrated, "state_bytes": self.size(), "state_limit_bytes": MAX_STATE_BYTES, "seconds": round(seconds, 1)}
        self._write_report(report)
        return report

    def export_progress(self) -> dict:
        """What this run has counted so far, for the stage that continues it in a new process. The entries themselves are in the file."""
        self.flush()
        return {"mode": self.mode, "stats": self.stats, "classes": self.classes, "members": {archive: sorted(ids) for archive, ids in self.members.items()},
                "reset_reason": self.reset_reason, "started": self.started, "cutoff": dict(self.cutoff), "rekeyed": dict(self.rekeyed), "migrated": self.migrated,
                "profile_digest": self.profile_digest, "people_digest": self.people_digest}

    def restore_progress(self, value: dict):
        """Take over the counts of the stage before. Raises ValueError for anything that is not exactly what export_progress gives."""
        try:
            if value["mode"] != self.mode:
                raise ValueError("the kept-results mode differs from the one the build stage used")
            layers = ("ingest", "rows", "scorecards")
            stats = {layer: {key: int(value["stats"][layer][key]) for key in ("reused", "rebuilt", "corrupt")} for layer in layers}
            classes = {key: int(value["classes"][key]) for key in ("new", "corrected", "unchanged", "removed")}
            members = {str(archive): {int(i) for i in ids} for archive, ids in value["members"].items()}
            reason = value["reset_reason"]
            started = float(value["started"])
            cutoff = {"ingest_same_content": int(value["cutoff"]["ingest_same_content"])}
            rekeyed = {key: int(value["rekeyed"][key]) for key in ("rows", "scorecards")}
            migrated = value["migrated"]
            profile_digest, people_digest = value["profile_digest"], value["people_digest"]
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("the kept-results counts from the build stage are not valid") from error
        if reason is not None and not isinstance(reason, str):
            raise ValueError("the kept-results counts from the build stage are not valid")
        if not isinstance(profile_digest, str) or not isinstance(people_digest, str) or (migrated is not None and not isinstance(migrated, dict)):
            raise ValueError("the kept-results counts from the build stage are not valid")
        if any(n < 0 for layer in stats.values() for n in layer.values()) or any(n < 0 for n in classes.values()):
            raise ValueError("the kept-results counts from the build stage are not valid")
        self.stats, self.classes, self.members, self.reset_reason, self.started = stats, classes, members, reason, started
        self.cutoff, self.rekeyed, self.migrated, self.profile_digest, self.people_digest = cutoff, rekeyed, migrated, profile_digest, people_digest
        self.load_ingest_keys()

    def close(self):
        self.flush()
        self.db.close()


@contextlib.contextmanager
def active(folder: Path, mode: str = "use"):
    """Make a State the current one while a build runs. Pass `mode="populate"` for the full-rebuild fallback."""
    global _current
    state = State(folder, mode)
    _current = state
    try:
        yield state
    finally:
        _current = None
        state.close()
