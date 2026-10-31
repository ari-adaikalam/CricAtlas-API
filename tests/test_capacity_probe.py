"""The capacity probe's parsing and arithmetic (loadtest/vps/capacity_probe.py) on fixture text shaped like the real cgroup, /proc and metrics files, with answers worked out by hand."""
import sys
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import capacity_probe as cp  # noqa: E402

CPU_STAT = "usage_usec 5000000\nuser_usec 4000000\nsystem_usec 1000000\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
METRICS = '''# HELP x y
cricket_api_admission_running{class="light"} 1
cricket_api_admission_running{class="heavy"} 2
cricket_api_admission_waiting{class="medium"} 3
cricket_api_admission_total{class="heavy",outcome="refused_room"} 7
cricket_api_admission_total{class="heavy",outcome="refused_wait"} 5
cricket_api_admission_total{class="heavy",outcome="admitted"} 100
cricket_api_admission_total{class="medium",outcome="refused_client"} 2
cricket_api_requests_in_flight 6
cricket_api_admission_units 4
'''


def fake(tmp_path, cpu_usec=5_000_000, busy=300, total=1000, psi_cpu=1.5, anon=300 * 1048576, refused=12):
    cg, proc = tmp_path / "cg", tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    cg.mkdir(exist_ok=True)
    (cg / "cpu.stat").write_text(CPU_STAT.replace("5000000", str(cpu_usec), 1))
    (cg / "cpu.pressure").write_text("some avg10=2.50 avg60=1.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    (cg / "memory.stat").write_text(f"anon {anon}\nfile 1048576\n")
    (cg / "memory.current").write_text(f"{anon + 1048576}\n")
    (cg / "memory.peak").write_text("1048576000\n")
    (cg / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    (proc / "stat").write_text(f"cpu  {busy} 0 0 {total - busy} 0 0 0 0 0 0\ncpu0 1 1 1 1 1 1 1 1 1 1\n")
    (proc / "pressure" / "cpu").write_text(f"some avg10={psi_cpu:.2f} avg60=0.00 avg300=0.00 total=1\n")
    (proc / "pressure" / "memory").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
    (proc / "pressure" / "io").write_text("some avg10=0.10 avg60=0.00 avg300=0.00 total=1\n")
    (proc / "loadavg").write_text("1.75 1.50 1.25 2/300 1234\n")
    (proc / "meminfo").write_text("MemTotal:  3906228 kB\nMemAvailable:    2700000 kB\n")
    return cg, proc


def test_the_files_are_parsed_into_the_columns(tmp_path):
    cg, proc = fake(tmp_path)
    row = cp.sample(cg, METRICS, 1000.0, proc)
    assert row["api_cpu_usec"] == 5_000_000 and row["api_nr_throttled"] == 0 and row["api_cpu_psi_some"] == 2.5 and row["api_mem_anon"] == 300 * 1048576
    assert row["host_busy_j"] == 300 and row["host_total_j"] == 1000 and row["host_cpu_psi_some"] == 1.5 and row["host_io_psi_some"] == 0.1 and row["load1"] == 1.75 and row["host_avail_kb"] == 2_700_000
    assert row["admission_running_light"] == 1 and row["admission_running_heavy"] == 2 and row["admission_waiting_medium"] == 3 and row["in_flight"] == 6 and row["admission_units"] == 4
    assert row["refused_heavy"] == 12 and row["refused_medium"] == 2 and row["refused_light"] == 0, "refusals of every kind are summed per class and admissions are not counted"
    assert set(row) == set(cp.COLUMNS)


def test_rates_between_two_samples_are_worked_out_exactly(tmp_path):
    cg, proc = fake(tmp_path)
    a = cp.sample(cg, METRICS, 1000.0, proc)
    cg, proc = fake(tmp_path, cpu_usec=5_000_000 + 12_000_000, busy=300 + 190, total=1000 + 400, refused=12)
    b = cp.sample(cg, METRICS.replace("refused_room\"} 7", "refused_room\"} 37"), 1010.0, proc)
    s = cp.summarize(a, b, cpus=2)
    assert s["seconds"] == 10.0 and s["api_cpu_pct_of_host"] == 60.0, "12 CPU seconds in 10 s on 2 CPUs"
    assert s["host_cpu_busy_pct"] == pytest.approx(100 * 190 / 400, abs=0.1) and s["refused_heavy_per_s"] == 3.0 and s["api_throttled_periods"] == 0
    with pytest.raises(ValueError):
        cp.summarize(b, a)


def test_a_missing_file_gives_zeros_not_a_crash(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    row = cp.sample(empty, "", 5.0, empty)
    assert row["api_cpu_usec"] == 0 and row["host_avail_kb"] == 0 and row["refused_heavy"] == 0


def test_the_command_writes_a_csv_and_never_overwrites_evidence(tmp_path):
    cg, proc = fake(tmp_path)
    out = tmp_path / "probe.csv"
    sample_rows = [cp.sample(cg, METRICS, 1.0, proc)]
    out.write_text("keep")
    assert cp.main(["--out", str(out), "--cgroup", str(cg), "--seconds", "1", "--interval", "1"]) == 2 and out.read_text() == "keep"
