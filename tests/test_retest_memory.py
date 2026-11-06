"""The memory rules of the Gate 1 protocol (loadtest/vps/retest_memory.py): the warm-up levels off when a block is within 3 percent of the one before, the baseline and the final level are the last five minutes,
a growth over 10 percent fails, a fall does not, and a warm-up that never levelled off can never give a pass."""
import json
import sys
from pathlib import Path

import pytest

VPS = Path(__file__).resolve().parents[1] / "loadtest" / "vps"
sys.path.insert(0, str(VPS))
import retest_memory as rm  # noqa: E402

MIB = 1048576


def run_dir(tmp_path, name, levels, step=5.0, start=1000.0):
    """A probe.csv whose anonymous memory goes through `levels` (MiB), evenly spread over the samples."""
    d = tmp_path / name
    d.mkdir()
    n = len(levels)
    with open(d / "probe.csv", "w") as f:
        f.write("t,api_mem_anon\n")
        for i, v in enumerate(levels):
            f.write(f"{start + i * step:.3f},{int(v * MIB)}\n")
    return d


def test_the_mean_is_over_the_last_five_minutes_only(tmp_path):
    d = run_dir(tmp_path, "a", [100] * 60 + [200] * 60)           # 120 samples of 5 s = 10 minutes: the last 5 minutes are all 200
    assert rm.mean_last(d, 300) == pytest.approx(200, abs=3)
    assert rm.mean_last(d, 600) == pytest.approx(150, abs=3)


@pytest.mark.parametrize("prev,cur,levelled", [(150, 152, True), (150, 154.4, True), (150, 155, False), (150, 140, False), (150, 146, True)])
def test_the_warm_up_levels_off_within_three_percent(tmp_path, prev, cur, levelled):
    a, b = run_dir(tmp_path, "p", [prev] * 60), run_dir(tmp_path, "c", [cur] * 60)
    assert (rm.main(["plateau", "--prev", str(a), "--cur", str(b)]) == 0) is levelled


def test_the_verdict_passes_within_ten_percent_fails_above_it_and_a_fall_is_not_a_failure(tmp_path):
    warm = run_dir(tmp_path, "w", [180] * 60)
    for final, expect in ((190, "PASS"), (198, "PASS"), (199, "FAIL"), (260, "FAIL"), (120, "PASS")):
        idle = run_dir(tmp_path, f"i{final}", [final] * 60)
        v = rm.verdict(warm, idle, True)
        assert v["verdict"].startswith(expect), (final, v)
    assert rm.verdict(warm, run_dir(tmp_path, "i199b", [199] * 60), True)["change_pct"] == pytest.approx(10.6, abs=0.2)


def test_a_warm_up_that_never_levelled_off_is_unverified_never_a_pass(tmp_path):
    warm, idle = run_dir(tmp_path, "w", [180] * 60), run_dir(tmp_path, "i", [181] * 60)
    assert rm.verdict(warm, idle, False)["verdict"].startswith("UNVERIFIED")
    big = rm.verdict(warm, run_dir(tmp_path, "i2", [400] * 60), False)
    assert big["verdict"].startswith("UNVERIFIED"), "a large rise is reported as it is but the verdict is not upgraded or downgraded by it"


def test_the_loaded_level_is_reported_beside_the_verdict_and_not_judged(tmp_path):
    warm, idle, load = run_dir(tmp_path, "w", [180] * 60), run_dir(tmp_path, "i", [185] * 60), run_dir(tmp_path, "l", [260] * 60)
    out = tmp_path / "out.json"
    assert rm.main(["verdict", "--warmup", str(warm), "--idle", str(idle), "--reached", "yes", "--confirm-load", str(load), "--json", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["loaded_level_of_the_confirmation_mib"] == pytest.approx(260, abs=1) and data["verdict"].startswith("PASS")


def test_a_run_with_no_samples_is_refused(tmp_path):
    d = tmp_path / "e"
    d.mkdir()
    (d / "probe.csv").write_text("t,api_mem_anon\n")
    with pytest.raises(SystemExit):
        rm.mean_last(d)
