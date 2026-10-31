"""The scratch settings: where the query engine may spill to disk, and how much.

These check the settings and how they are applied: that a value that cannot work stops the start with a message, that a good one reaches the engine before its configuration is locked, that it stays
locked, and that a failure is handled without leaking the engine or an exception message. They work on disposable copies of the data and on temporary folders.

They do NOT show that the engine really spills into the folder, that the cap really stops a spill, what a full scratch volume does to a running request, or that the server recovers afterwards. A test
cannot show that without a filesystem of a fixed size and a query that really exceeds its memory, so those are deployment gates, run on the host. The failures simulated below are raised by a
stand-in, to check what the server does with them, and prove nothing about when the engine raises them.
"""
import logging
import os
import shutil
from collections import namedtuple

import duckdb
import pytest

from cricket_api import scratch, serve
from cricket_api import db as db_module
from cricket_api.db import Database
from cricket_api.settings import ConfigError, Settings

from conftest import TEST_SECRET

GIB, MIB = 1024 ** 3, 1024 ** 2


@pytest.fixture
def sandbox(dataset, tmp_path):
    """A private copy of the data folder and a sibling folder for scratch, so no test touches the shared fixture."""
    data = tmp_path / "data"
    shutil.copytree(dataset, data)
    folder = tmp_path / "scratch"
    folder.mkdir()
    return data, folder


def settings_for(data, folder=None, maximum="1GiB", **more):
    return Settings(data_dir=data, cursor_secret=TEST_SECRET, scratch_dir=None if folder is None else str(folder), scratch_max=maximum, **more)


def setting(db, name):
    cursor = db._con.cursor()
    try:
        return cursor.execute(f"SELECT current_setting('{name}')").fetchone()[0]
    finally:
        cursor.close()


def engine_text(size_bytes):
    """How the engine itself prints a size, read from a fresh connection, so the tests do not depend on its formatting."""
    con = duckdb.connect(":memory:")
    con.execute(f"SET max_temp_directory_size='{size_bytes}B'")
    return con.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]


# The size --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1GiB", GIB), ("1gib", GIB), (" 1 GiB ", GIB), ("512MiB", 512 * MIB), ("1GB", 10 ** 9), ("1.5GiB", 3 * GIB // 2), ("64MiB", 64 * MIB), ("100GiB", 100 * GIB),
    ("67108864B", 64 * MIB), ("1000MB", 10 ** 9), ("2048MiB", 2 * GIB), ("1.0gb", 10 ** 9)])
def test_a_good_size_is_read_as_the_engine_reads_it(text, expected):
    assert scratch.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", " ", "abc", "1", "GiB", "-1GiB", "+1GiB", "1 PB", "1PiB", "unlimited", "1e9", "1GiB extra", "0GiB", "63MiB", "101GiB", "1,5GiB", "1.GiB", ".5GiB",
                                  "١GiB", "１GiB", "nan", "inf", "1GiBGiB", "1 GiB 2", "9" * 40 + "GiB", "0x10GiB", "1G", "1g", "1Gi"])
def test_a_bad_size_stops_the_start_and_the_message_names_the_setting(text):
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH_MAX"):
        scratch.parse_size(text)


@pytest.mark.parametrize("value", [None, 5, 1.5, b"1GiB", ["1GiB"]])
def test_a_value_that_is_not_text_is_refused(value):
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH_MAX"):
        scratch.parse_size(value)


def test_the_message_never_repeats_a_long_value():
    junk = "zzCANARYzz" * 50
    with pytest.raises(ConfigError) as error:
        scratch.parse_size(junk)
    assert len(str(error.value)) < 200 and junk not in str(error.value)


def test_the_limits_are_inclusive_at_both_ends():
    assert scratch.parse_size("64MiB") == scratch.MIN_BYTES and scratch.parse_size("100GiB") == scratch.MAX_BYTES
    for text in ("64MiB".replace("64", "63.9"), "100.1GiB"):
        with pytest.raises(ConfigError):
            scratch.parse_size(text)


# Precision, length and overflow ------------------------------------------------------------------------------------------

MIN, MAX = 64 * MIB, 100 * GIB


@pytest.mark.parametrize("text, expected", [
    # the lower limit, exactly and one byte either side
    ("64MiB", MIN), ("67108864B", MIN), ("65536KiB", MIN), ("0.0625GiB", MIN), ("67.108864MB", MIN), ("67108864.9B", MIN), ("64.0000001MiB", MIN),
    # the upper limit
    ("100GiB", MAX), ("107374182400B", MAX), ("102400MiB", MAX), ("107.3741824GB", MAX), ("100.0000000001GiB", MAX), ("107374182400.9B", MAX),
    # just inside, away from the limits, and a number with many digits that is still small
    ("67108865B", MIN + 1), ("107374182399B", MAX - 1), ("0000000000000000000000001GiB", GIB), ("1.000000000000000000000000GiB", GIB), ("1.5000KiB".replace("1.5000KiB", "65536.5KiB"), 65536 * 1024 + 512),
])
def test_the_limits_are_exact_to_the_byte(text, expected):
    assert scratch.parse_size(text) == expected


@pytest.mark.parametrize("text", [
    "67108863B", "65535KiB", "63.999999MiB", "67.108863MB", "67108863.9B", "0.0624999GiB", "0B", "0.9B", "1B",
    "107374182401B", "102401MiB", "100.0000001GiB", "107.374182401GB", "107374182401.5B", "100.1GiB", "101GiB", "1TiB", "1TB"])
def test_a_value_one_step_outside_a_limit_is_refused(text):
    with pytest.raises(ConfigError, match="from 64MiB to 100GiB"):
        scratch.parse_size(text)


def test_a_fraction_of_a_byte_is_rounded_down_and_never_up():
    """67108863.9 bytes is 67108863, below the minimum. Rounding to the nearest would let it through."""
    assert scratch.parse_size("67108864.9B") == MIN
    with pytest.raises(ConfigError):
        scratch.parse_size("67108863.9B")
    assert scratch.parse_size("107374182400.9B") == MAX
    with pytest.raises(ConfigError):
        scratch.parse_size("107374182401.0B")


def test_the_result_is_a_whole_number_made_without_floating_point():
    """A float cannot tell these apart: 100.00000000001GiB is above 100GiB by 10 bytes, and a double holds only about 16 digits of 1.07e11 in the middle of a product."""
    assert scratch.parse_size("107374182400B") == MAX and type(scratch.parse_size("1GiB")) is int
    with pytest.raises(ConfigError):
        scratch.parse_size("107374182410B")
    with pytest.raises(ConfigError):
        scratch.parse_size("100.00000001GiB")                 # 107 bytes over the limit, which a float of the whole product can round away


@pytest.mark.parametrize("text, exact", [("67.1GB", 67_100_000_000), ("68.1GB", 68_100_000_000), ("64.1GB", 64_100_000_000), ("2.09GB", 2_090_000_000), ("65.74GB", 65_740_000_000),
                                         ("67.13GB", 67_130_000_000), ("68.60GB", 68_600_000_000)])
def test_values_a_float_gets_wrong_by_a_byte_are_exact(text, exact):
    """Found by search: int(float('67.1') * 10**9) is 67099999999, one byte short of 67.1 gigabytes."""
    assert int(float(text[:-2]) * 10 ** 9) == exact - 1                      # the float answer is wrong, which is why it is not used
    assert scratch.parse_size(text) == exact


def test_a_malformed_value_is_echoed_to_at_most_thirty_characters():
    for junk in ("a" * 32, "1" + "x" * 31, "G" * 32):
        with pytest.raises(ConfigError, match="must be a size") as error:
            scratch.parse_size(junk)
        assert junk[:31] not in str(error.value) and junk[:30] in str(error.value)


@pytest.mark.parametrize("text", ["9" * 31 + "B", "9" * 25 + "TiB", "1" + "0" * 28 + "GiB", "9" * 29 + "TB", "1" + "0" * 20 + "B", "99999999999999999999999999TiB"])
def test_a_number_that_would_overflow_any_fixed_width_is_refused_as_too_large(text):
    assert len(text) <= scratch.MAX_INPUT
    with pytest.raises(ConfigError, match="from 64MiB to 100GiB"):
        scratch.parse_size(text)


@pytest.mark.parametrize("length", [33, 34, 100, 1000, 5000, 100_000])
def test_an_overlong_input_is_refused_before_it_is_read_and_is_not_repeated(length):
    junk = "9" * (length - 3) + "GiB"
    with pytest.raises(ConfigError, match="too long") as error:
        scratch.parse_size(junk)
    assert "9999" not in str(error.value) and len(str(error.value)) < 150


def test_the_length_limit_is_exact():
    ok = "0" * (scratch.MAX_INPUT - 4) + "1GiB"
    assert len(ok) == scratch.MAX_INPUT and scratch.parse_size(ok) == GIB
    with pytest.raises(ConfigError, match="too long"):
        scratch.parse_size("0" + ok)
    padded = " " * (scratch.MAX_INPUT - 4) + "1GiB"                  # padding counts: the limit is on the input as given
    assert scratch.parse_size(padded) == GIB
    with pytest.raises(ConfigError, match="too long"):
        scratch.parse_size(" " + padded)


@pytest.mark.parametrize("text", ["1GiB\n", "\n1GiB", "1\nGiB", "1GiB\r", "1\u00a0GiB", "1\u2009GiB", "1\u3000GiB", "1\x0bGiB", "1\x0cGiB", "1GiB\x00", "\x001GiB", "1\x00GiB"])
def test_only_spaces_and_tabs_may_surround_the_parts(text):
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH_MAX"):
        scratch.parse_size(text)


@pytest.mark.parametrize("text, expected", [(" 1GiB", GIB), ("1GiB ", GIB), ("\t1GiB\t", GIB), ("1 \t GiB", GIB), ("  1.5  GiB  ", 3 * GIB // 2)])
def test_spaces_and_tabs_are_allowed(text, expected):
    assert scratch.parse_size(text) == expected


@pytest.mark.parametrize("text", ["1GiВ", "1ＧiB", "1G\u0130B", "1\u0663GiB", "1.\u0665GiB", "1,5GiB", "1_000MiB", "1e3MiB", "1E3MiB", "0b1MiB", "0o7MiB", "1.2.3GiB", "..5GiB", "1..5GiB", "1-1GiB", "1+1GiB",
                                  "1/2GiB", "(1)GiB", "1GiB;", "1GiB,", "'1GiB'", "1GiB'", "１０ＧｉＢ", "1MiB/s"])
def test_text_that_only_looks_like_a_size_is_refused(text):
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH_MAX"):
        scratch.parse_size(text)


@pytest.mark.parametrize("value", [True, False, 5, 1.5, None, b"1GiB", bytearray(b"1GiB"), ["1GiB"], ("1GiB",), {"a": 1}, object(), float("nan"), float("inf")])
def test_a_value_that_is_not_text_is_a_configuration_error_and_names_only_its_type(value):
    with pytest.raises(ConfigError) as error:
        scratch.parse_size(value)
    assert "CRICKET_API_SCRATCH_MAX" in str(error.value) and "object at 0x" not in str(error.value)


def test_every_refusal_is_a_configuration_error_and_nothing_else_escapes():
    hostile = ["", " ", "\x00", "9" * 100_000, "1" + "." * 40, "-" * 40, "٣" * 10 + "GiB", "1" * 33 + "B", "GiB", "B", ".", ". ", "0", "00", "0.0", "0GiB", "1 2", "1 GiB GiB", "\U0001f600", "1\U0001f600B"]
    for text in hostile:
        try:
            scratch.parse_size(text)
        except ConfigError:
            continue
        pytest.fail(f"{text[:20]!r} did not raise ConfigError")


def test_the_arithmetic_agrees_with_an_independent_exact_calculation():
    from fractions import Fraction
    for number in ("1", "0.5", "64", "63.999", "100", "100.0001", "1.234567891", "0.0625", "99.9999999999"):
        for unit, factor in (("B", 1), ("KB", 1000), ("MB", 10 ** 6), ("GB", 10 ** 9), ("KiB", 1024), ("MiB", 1024 ** 2), ("GiB", 1024 ** 3)):
            exact = Fraction(number) * factor
            floor = exact.numerator // exact.denominator
            if scratch.MIN_BYTES <= floor <= scratch.MAX_BYTES:
                assert scratch.parse_size(number + unit) == floor, number + unit
            else:
                with pytest.raises(ConfigError):
                    scratch.parse_size(number + unit)


def test_the_launcher_stops_on_an_oversized_or_out_of_range_size_with_exit_code_2_and_no_traceback(monkeypatch, capsys):
    import uvicorn
    from pathlib import Path
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(options))
    for key in [k for k in os.environ if k.startswith("CRICKET_API_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("CRICKET_API_DATA", str(Path(__file__).parent / "golden" / "api"))
    monkeypatch.setenv("CRICKET_API_SECRET", TEST_SECRET)
    for value in ("9" * 5000 + "GiB", "9" * 31 + "B", "99999999999999999999999999TiB", "67108863B", "107374182401B", "100.0000001GiB", "1e3GiB", "1GiB\n", ""):
        monkeypatch.setenv("CRICKET_API_SCRATCH_MAX", value)
        assert serve.main(["--port", "9"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("cricket-api:") and "CRICKET_API_SCRATCH_MAX" in err and "Traceback" not in err
        assert len(err) < 200 and (len(value) <= 30 or value[:31] not in err)                 # at most 30 characters of the value are echoed
    for value in ("67108864B", "107374182400B", "64MiB", "100GiB"):                        # the limits themselves start
        monkeypatch.setenv("CRICKET_API_SCRATCH_MAX", value)
        assert serve.main(["--port", "9"]) == 0
        calls.clear()
    assert calls == []


def test_the_real_launcher_stops_on_an_oversized_size_and_a_value_just_outside_the_limit():
    from test_launcher import Stopped, Launched
    for value in ("9" * 5000 + "GiB", "9" * 31 + "B", "67108863B", "107374182401B"):
        with pytest.raises(Stopped) as stopped:
            Launched({"CRICKET_API_SCRATCH_MAX": value})
        out = stopped.value.output
        assert stopped.value.code == 2 and "CRICKET_API_SCRATCH_MAX" in out and "Traceback" not in out and stopped.value.never_started_uvicorn()
        assert len(out) < 200 and (len(value) <= 30 or value[:31] not in out)


@pytest.mark.parametrize("value", ["67108864B", "107374182400B"])
def test_the_real_server_starts_at_each_limit(value):
    from test_launcher import Launched
    server = Launched({"CRICKET_API_SCRATCH_MAX": value})
    try:
        with server.client() as c:
            assert c.get("/v1/meta").status_code == 200
    finally:
        server.stop()


# The plan --------------------------------------------------------------------------------------------------------------------

def test_with_no_folder_the_engines_own_is_kept_and_the_default_cap_applies(sandbox):
    data, _ = sandbox
    assert scratch.plan(settings_for(data)) == scratch.Plan(None, GIB)


def test_a_good_folder_is_resolved(sandbox):
    data, folder = sandbox
    link = folder.parent / "link"
    link.symlink_to(folder)
    for given in (folder, str(folder) + "/", link):
        plan = scratch.plan(settings_for(data, given, "256MiB"))
        assert plan.directory == folder.resolve() and plan.max_bytes == 256 * MIB and plan.max_text == f"{256 * MIB}B"


@pytest.mark.parametrize("make, text", [
    (lambda data, folder: folder.parent / "missing", "does not exist"),
    (lambda data, folder: folder.parent / "afile", "not a folder"),
    (lambda data, folder: "relative/scratch", "absolute"),
    (lambda data, folder: "", "empty"),
    (lambda data, folder: "   ", "empty"),
    (lambda data, folder: str(folder) + "\nmore", "control characters"),
    (lambda data, folder: str(folder) + "\x00", "control characters"),
    (lambda data, folder: str(folder) + "\x7f", "control characters"),
    (lambda data, folder: data, "apart from the data folder"),
    (lambda data, folder: data / "sub", "apart from the data folder"),
    (lambda data, folder: data.parent, "apart from the data folder"),
    (lambda data, folder: data.parent.parent, "apart from the data folder"),
], ids=["missing", "file", "relative", "empty", "blank", "newline", "nul", "delete-char", "the-data-folder", "inside-the-data", "holds-the-data", "far-above-the-data"])
def test_a_folder_that_cannot_work_stops_the_start(sandbox, make, text):
    data, folder = sandbox
    (folder.parent / "afile").write_text("x")
    (data / "sub").mkdir(exist_ok=True)
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH") as error:
        scratch.plan(settings_for(data, make(data, folder)))
    assert text in str(error.value)


def test_a_symlink_to_the_data_folder_is_the_data_folder(sandbox):
    data, folder = sandbox
    link = folder / "looks-apart"
    link.symlink_to(data)
    with pytest.raises(ConfigError, match="apart from the data folder"):
        scratch.plan(settings_for(data, link))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere, so a folder cannot be made unwritable for it")
def test_a_folder_the_process_cannot_write_to_stops_the_start(sandbox):
    data, folder = sandbox
    folder.chmod(0o500)
    try:
        with pytest.raises(ConfigError, match="not writable"):
            scratch.plan(settings_for(data, folder))
    finally:
        folder.chmod(0o700)


def test_the_check_leaves_nothing_behind(sandbox):
    data, folder = sandbox
    scratch.plan(settings_for(data, folder))
    assert list(folder.iterdir()) == []


def test_a_long_path_is_not_repeated_in_full(sandbox):
    data, folder = sandbox
    with pytest.raises(ConfigError) as error:
        scratch.plan(settings_for(data, "/" + "zzCANARYzz/" * 40))
    assert len(str(error.value)) < 400


Disk = namedtuple("Disk", "total used free")


def test_the_cap_must_leave_room_on_the_volume_so_the_engine_refuses_before_it_is_full(sandbox, monkeypatch):
    data, folder = sandbox
    monkeypatch.setattr(scratch.shutil, "disk_usage", lambda path: Disk(2 * GIB, 0, 2 * GIB))
    assert scratch.plan(settings_for(data, folder, "1GiB")).max_bytes == GIB                         # half of the volume
    assert scratch.plan(settings_for(data, folder, "1.6GiB")).max_bytes == int(1.6 * GIB)            # exactly 80 percent is allowed
    for size in ("1.7GiB", "2GiB", "10GiB"):
        with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH_MAX .* percent"):
            scratch.plan(settings_for(data, folder, size))


def test_the_volume_check_applies_only_when_a_folder_is_set(sandbox, monkeypatch):
    data, _ = sandbox
    monkeypatch.setattr(scratch.shutil, "disk_usage", lambda path: Disk(1, 0, 1))
    assert scratch.plan(settings_for(data, None, "10GiB")).max_bytes == 10 * GIB


def test_the_settings_are_read_from_the_environment(monkeypatch):
    for name in ("CRICKET_API_SCRATCH", "CRICKET_API_SCRATCH_MAX"):
        monkeypatch.delenv(name, raising=False)
    unset = Settings(cursor_secret=TEST_SECRET)
    assert unset.scratch_dir is None and unset.scratch_max == "1GiB"
    monkeypatch.setenv("CRICKET_API_SCRATCH", "/srv/scratch")
    monkeypatch.setenv("CRICKET_API_SCRATCH_MAX", "512MiB")
    configured = Settings(cursor_secret=TEST_SECRET)
    assert configured.scratch_dir == "/srv/scratch" and configured.scratch_max == "512MiB"
    monkeypatch.setenv("CRICKET_API_SCRATCH", "")
    assert Settings(cursor_secret=TEST_SECRET).scratch_dir == ""                                     # present but empty is kept as it is, and refused by the plan, not replaced by the default


# What reaches the engine -------------------------------------------------------------------------------------------------

def test_the_folder_and_the_cap_are_applied_to_the_engine(sandbox):
    data, folder = sandbox
    db = Database(settings_for(data, folder, "256MiB"))
    try:
        assert setting(db, "temp_directory") == str(folder.resolve())
        assert setting(db, "max_temp_directory_size") == engine_text(256 * MIB) == "256.0 MiB"
    finally:
        db.close()


def test_without_a_folder_only_the_cap_is_applied(sandbox):
    data, _ = sandbox
    default_folder = duckdb.connect(":memory:").execute("SELECT current_setting('temp_directory')").fetchone()[0]
    db = Database(settings_for(data, None, "512MiB"))
    try:
        assert setting(db, "temp_directory") == default_folder and default_folder != ""
        assert setting(db, "max_temp_directory_size") == "512.0 MiB"                                 # not the engine's default of 90 percent of the free disk
    finally:
        db.close()


@pytest.mark.parametrize("name", ["o'brien", "scratch é 日本", "with space", 'a"b', "semi;colon", "a--b"])
def test_a_folder_with_an_awkward_name_is_applied_exactly(sandbox, name):
    data, folder = sandbox
    awkward = folder / name
    awkward.mkdir()
    db = Database(settings_for(data, awkward))
    try:
        assert setting(db, "temp_directory") == str(awkward.resolve())
    finally:
        db.close()


@pytest.mark.parametrize("statement", ["SET temp_directory='/tmp/elsewhere'", "SET max_temp_directory_size='90GB'", "RESET temp_directory", "RESET max_temp_directory_size",
                                       "SET max_temp_directory_size='10TB'"])
def test_the_scratch_settings_stay_locked_after_the_start(sandbox, statement):
    data, folder = sandbox
    db = Database(settings_for(data, folder))
    try:
        with pytest.raises(duckdb.Error, match="locked"):
            db._con.cursor().execute(statement)
        assert setting(db, "temp_directory") == str(folder.resolve()) and setting(db, "max_temp_directory_size") == "1.0 GiB"
    finally:
        db.close()


class Recorder:
    """Stands in for the engine's connection: passes everything through and keeps the statements in order."""
    def __init__(self, real, fail_on=None):
        self.real, self.statements, self.fail_on, self.closed = real, [], fail_on, False

    def execute(self, sql, *a, **k):
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise duckdb.IOException("zzCANARYTEXTzz /secret/path")
        return self.real.execute(sql, *a, **k)

    def cursor(self):
        return self.real.cursor()

    def close(self):
        self.closed = True
        self.real.close()


@pytest.fixture
def recorded(monkeypatch):
    made = []
    real_connect = duckdb.connect

    def connect(*args, fail_on=None, **kwargs):
        made.append(Recorder(real_connect(*args, **kwargs), fail_on))
        return made[-1]
    monkeypatch.setattr(db_module.duckdb, "connect", connect)
    return made


def test_the_scratch_settings_are_applied_before_external_access_is_closed_and_before_the_lock(sandbox, recorded):
    """The engine refuses to change its temporary directory once external access is off, so the order is not a matter of taste."""
    data, folder = sandbox
    Database(settings_for(data, folder)).close()
    statements = recorded[0].statements
    where = lambda start: next(i for i, s in enumerate(statements) if s.startswith(start))
    assert where("SET temp_directory") < where("SET enable_external_access") < where("SET lock_configuration")
    assert where("SET max_temp_directory_size") < where("SET enable_external_access") < where("SET lock_configuration")
    assert statements[-1] == "SET lock_configuration=true"


def test_no_folder_statement_is_sent_when_no_folder_is_set(sandbox, recorded):
    data, _ = sandbox
    Database(settings_for(data)).close()
    assert not [s for s in recorded[0].statements if s.startswith("SET temp_directory")]
    assert [s for s in recorded[0].statements if s.startswith("SET max_temp_directory_size")] == [f"SET max_temp_directory_size='{GIB}B'"]


# Failure handling ------------------------------------------------------------------------------------------------------------

def test_a_bad_scratch_setting_stops_the_start_before_any_data_file_is_read(tmp_path, monkeypatch):
    read = []
    monkeypatch.setattr(db_module, "load_meta", lambda *a, **k: read.append(1))
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH"):
        Database(Settings(data_dir=tmp_path / "nowhere", cursor_secret=TEST_SECRET, scratch_dir="relative", scratch_max="1GiB"))
    with pytest.raises(ConfigError, match="CRICKET_API_SCRATCH_MAX"):
        Database(Settings(data_dir=tmp_path / "nowhere", cursor_secret=TEST_SECRET, scratch_dir=None, scratch_max="lots"))
    assert read == []


def test_a_good_scratch_setting_does_not_hide_a_data_problem(tmp_path):
    from cricket_api.db import DataError
    folder = tmp_path / "scratch"
    folder.mkdir()
    with pytest.raises(DataError):
        Database(settings_for(tmp_path / "no-data", folder))


def test_an_engine_that_refuses_a_setting_stops_the_start_with_no_text_from_it_and_closes_the_connection(sandbox, recorded, monkeypatch):
    data, folder = sandbox
    real_connect = db_module.duckdb.connect
    monkeypatch.setattr(db_module.duckdb, "connect", lambda *a, **k: real_connect(*a, fail_on="SET temp_directory", **k))
    with pytest.raises(ConfigError) as error:
        Database(settings_for(data, folder))
    message = str(error.value)
    assert "IOException" in message and "CRICKET_API_SCRATCH" in message and "CANARY" not in message and "/secret" not in message
    assert recorded[-1].closed and error.value.__cause__ is None


def test_the_launcher_stops_with_a_message_and_no_traceback_and_never_reaches_the_server(monkeypatch, capsys, tmp_path):
    import uvicorn
    from pathlib import Path
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(options))
    for key in [k for k in os.environ if k.startswith("CRICKET_API_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("CRICKET_API_DATA", str(Path(__file__).parent / "golden" / "api"))
    monkeypatch.setenv("CRICKET_API_SECRET", TEST_SECRET)
    for name, value, text in (("CRICKET_API_SCRATCH", "relative/path", "must be an absolute path"), ("CRICKET_API_SCRATCH", str(tmp_path / "missing"), "does not exist"),
                              ("CRICKET_API_SCRATCH_MAX", "lots", "must be a size"), ("CRICKET_API_SCRATCH_MAX", "1KiB", "from 64MiB to 100GiB")):
        monkeypatch.setenv(name, value)
        assert serve.main(["--port", "9"]) == 2
        err = capsys.readouterr().err
        assert "cricket-api:" in err and name in err and text in err and "Traceback" not in err
        monkeypatch.delenv(name)
    assert calls == []


def test_the_launcher_starts_with_good_settings(monkeypatch, tmp_path):
    import uvicorn
    from pathlib import Path
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(app))
    for key in [k for k in os.environ if k.startswith("CRICKET_API_")]:
        monkeypatch.delenv(key)
    folder = tmp_path / "scratch"
    folder.mkdir()
    monkeypatch.setenv("CRICKET_API_DATA", str(Path(__file__).parent / "golden" / "api"))
    monkeypatch.setenv("CRICKET_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("CRICKET_API_SCRATCH", str(folder))
    monkeypatch.setenv("CRICKET_API_SCRATCH_MAX", "128MiB")
    assert serve.main(["--port", "9"]) == 0 and len(calls) == 1
    assert setting(calls[0].state.db, "temp_directory") == str(folder.resolve())
    assert setting(calls[0].state.db, "max_temp_directory_size") == "128.0 MiB"
    calls[0].state.db.close()


def test_the_real_launcher_refuses_a_bad_scratch_folder_before_it_starts_the_server():
    from test_launcher import Stopped, Launched
    for env, text in (({"CRICKET_API_SCRATCH": "relative"}, "absolute"), ({"CRICKET_API_SCRATCH": "/nonexistent-scratch-folder"}, "does not exist"), ({"CRICKET_API_SCRATCH_MAX": "x"}, "must be a size")):
        with pytest.raises(Stopped) as stopped:
            Launched(env)
        assert stopped.value.code == 2 and text in stopped.value.output and "Traceback" not in stopped.value.output and stopped.value.never_started_uvicorn()


def test_the_real_server_starts_and_answers_with_a_scratch_folder(tmp_path):
    from test_launcher import Launched
    folder = tmp_path / "scratch"
    folder.mkdir()
    server = Launched({"CRICKET_API_SCRATCH": str(folder), "CRICKET_API_SCRATCH_MAX": "128MiB"})
    try:
        with server.client() as c:
            assert c.get("/v1/meta").status_code == 200 and c.get("/v1/teams").status_code == 200
    finally:
        server.stop()


@pytest.mark.parametrize("error", [duckdb.OutOfMemoryException("zzCANARYTEXTzz max_temp_directory_size reached"), duckdb.IOException("zzCANARYTEXTzz No space left on device"),
                                   duckdb.Error("zzCANARYTEXTzz")], ids=["out-of-memory", "io", "engine"])
def test_a_scratch_style_failure_in_one_query_is_a_plain_500_and_the_next_request_is_served(make_client, monkeypatch, caplog, error):
    """The error is raised by a stand-in. This shows what the server does with it, not when the engine raises it."""
    client = make_client()
    real_query = client.app.state.db.query
    monkeypatch.setattr(client.app.state.db, "query", lambda *a, **k: (_ for _ in ()).throw(error))
    with caplog.at_level(logging.ERROR, logger="cricket_api.faults"):
        failed = client.get("/v1/teams")
    assert failed.status_code == 500 and failed.headers["content-type"].startswith("application/problem+json") and failed.headers["x-request-id"]
    assert "CANARY" not in failed.text + caplog.text + "".join(failed.headers.values()) and "temp" not in failed.text.lower() and "space" not in failed.text.lower()
    assert failed.headers["x-request-id"] in caplog.text and "diagnosis:" in caplog.text
    assert client.get("/health").status_code == 200 and client.get("/v1/meta").status_code == 200
    monkeypatch.setattr(client.app.state.db, "query", real_query)
    assert client.get("/v1/teams").status_code == 200                                                # the failure was one request's, and nothing stayed broken
    text = client.app.state.telemetry.render().decode()
    assert 'cricket_api_requests_total{route="/v1/teams",method="GET",status="500"} 1' in text and "CANARY" not in text
    gate = client.app.state.gate
    assert gate.total == 0 and gate.counts["admitted"] == gate.counts["closed"]                      # the admission unit of the failed request was given back
