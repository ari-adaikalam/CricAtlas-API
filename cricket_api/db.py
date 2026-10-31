"""The data layer: DuckDB over the API's Parquet files, with external access restricted.

At start the files are checked against the hashes in meta.json, the views are made, and the connection is restricted: file access is limited
to the data folder, extensions cannot be installed or loaded, and the configuration is locked. Inside the data folder the connection can
still read and write files, so the deployment must mount that folder read-only. Every query runs on its own cursor with a time limit and a
cap on how many run at once.
"""
import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

from cricket_api import budget, contract, scratch
from cricket_api.errors import ApiError
from cricket_api.settings import ConfigError, Settings


class DataError(RuntimeError):
    """The data folder is missing, changed or not what meta.json says. The server refuses to start."""


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 23), b""):
            digest.update(block)
    return digest.hexdigest()


def load_meta(data_dir: Path, verify: bool = True) -> dict:
    path = data_dir / "meta.json"
    if not path.exists():
        raise DataError("meta.json is missing, write the data files first")
    meta = json.loads(path.read_text(encoding="utf-8"))
    known = {spec["source"].removesuffix(".csv") for spec in contract.csv_tables().values()}
    for name, table in meta["tables"].items():
        if name not in contract.TABLES:
            raise DataError(f"meta.json names a table that is not in the contract: {name}")
        parquet = data_dir / table["file"]
        if not parquet.exists():
            raise DataError(f"{table['file']} is missing")
        if verify and file_hash(parquet) != table["sha256"]:
            raise DataError(f"{table['file']} does not match its hash in meta.json")
    check_compatible(meta)
    return meta


def check_compatible(meta: dict) -> None:
    """The data must be the shape this code reads: every table of the contract is there, with the columns the contract names, in order. The server then refuses to start on data built for another version of the API
    (an older export without the derived tables, or a newer one with tables this code does not know) instead of starting and failing on the first request that reads what is not there."""
    missing = sorted(set(contract.TABLES) - set(meta["tables"]))
    if missing:
        raise DataError(f"the data was built for another version of the API: these tables are missing: {', '.join(missing)}. Rebuild it with this version's export")
    for name, table in meta["tables"].items():
        want = list(contract.TABLES[name]["columns"])
        if list(table["columns"]) != want:
            raise DataError(f"the data was built for another version of the API: table {name} has other columns than this code reads "
                            f"(missing {sorted(set(want) - set(table['columns']))[:5]}, unexpected {sorted(set(table['columns']) - set(want))[:5]})")


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.scratch = scratch.plan(settings)               # before any file is read: a scratch setting that cannot work stops the start
        self.meta = load_meta(settings.data_dir, settings.verify_files)
        self.tables = list(self.meta["tables"])
        self.features = self.meta.get("features", {})
        self.columns = {name: list(table["columns"]) for name, table in self.meta["tables"].items()}
        self._con = duckdb.connect(":memory:")
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_queries)
        try:
            self._lock_down()
        except duckdb.Error as error:                       # the engine refused a setting: say which kind of failure it was, close it, and stop the start as a configuration problem
            self._con.close()
            raise ConfigError(f"the query engine would not accept its settings ({type(error).__name__}): check CRICKET_API_SCRATCH and CRICKET_API_MEMORY") from None
        self._tally = threading.Lock()                  # queries run on worker threads, and `+= 1` on an attribute is not atomic
        self.queries = self.timeouts = self.busy = 0     # run, stopped by the time limit, refused because every query slot stayed taken

    def _bump(self, name: str) -> None:
        with self._tally:
            setattr(self, name, getattr(self, name) + 1)

    @property
    def data_version(self) -> str:
        return self.meta["data_version"]

    def _lock_down(self) -> None:
        folder = str(self.settings.data_dir.resolve()).replace("'", "''")
        for name, table in self.meta["tables"].items():
            path = str((self.settings.data_dir / table["file"]).resolve()).replace("'", "''")
            self._con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet(\'{path}\')')
        # The temporary directory is set first. Once enable_external_access is false the engine refuses to change it, and once the configuration is locked it refuses every change.
        spill = [f"SET max_temp_directory_size='{self.scratch.max_text}'"]
        if self.scratch.directory is not None:
            spill.insert(0, "SET temp_directory='" + str(self.scratch.directory).replace("'", "''") + "'")
        for statement in (*spill, f"SET allowed_directories=['{folder}']", "SET enable_external_access=false", "SET autoinstall_known_extensions=false",
                          "SET autoload_known_extensions=false", f"SET memory_limit='{self.settings.memory_limit}'", f"SET threads={int(self.settings.threads)}",
                          "SET lock_configuration=true"):
            self._con.execute(statement)

    @contextmanager
    def _timed(self, cursor, seconds: float):
        timer = threading.Timer(seconds, cursor.interrupt)
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def _allowance(self, spent) -> float:
        """The seconds a query may run: the query limit, cut to what the request has left. Raises if the client has left or the request's time is spent."""
        if spent is None:
            return self.settings.query_seconds
        if spent.gone.is_set():                             # the client has left: do not start more work for it
            raise ApiError(503, "The request was abandoned", headers={"Retry-After": "1"})
        if spent.spent():
            self._bump("timeouts")
            raise ApiError(503, "That request took too long. Narrow it with a filter", headers={"Retry-After": "5"})
        return min(self.settings.query_seconds, spent.left())

    def query(self, sql: str, params: list | tuple = ()) -> list[dict]:
        """Run one read query. The SQL is written in this package, and every value arrives as a bound parameter."""
        spent = budget.current()
        limit = self._allowance(spent)
        wait = self.settings.queue_seconds if spent is None else max(0.0, min(self.settings.queue_seconds, spent.left()))
        if not self._slots.acquire(timeout=wait):
            if spent is not None and spent.spent():         # the request's time ran out while it waited: that is a timeout, not a busy server
                self._bump("timeouts")
                raise ApiError(503, "That request took too long. Narrow it with a filter", headers={"Retry-After": "5"})
            self._bump("busy")
            raise ApiError(503, "The server is busy, try again in a moment", headers={"Retry-After": "2"})
        try:
            limit = self._allowance(spent)                  # the wait took time and the client may have left during it: look again before any cursor or statement exists
        except BaseException:
            self._slots.release()
            raise
        cursor = self._con.cursor()
        try:
            with self._timed(cursor, max(0.001, limit)):
                result = cursor.execute(sql, list(params))
                names = [d[0] for d in result.description]
                rows = result.fetchall()
            self._bump("queries")
            return [dict(zip(names, row)) for row in rows]
        except duckdb.InterruptException:
            self._bump("timeouts")
            raise ApiError(503, "That query took too long. Narrow it with a filter", headers={"Retry-After": "5"}) from None
        finally:
            cursor.close()
            self._slots.release()

    def close(self) -> None:
        self._con.close()
