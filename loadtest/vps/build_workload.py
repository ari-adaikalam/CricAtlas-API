#!/usr/bin/env python3
"""Build the workload file of the `final` mix (socket_load.py) from an export folder, so the validation run asks for the shapes that matter and not only the first ids a search returns.

    build_workload.py --data DIR --out workload.json [--seed N]

players          careers of every size: the heaviest (most innings), the middle, the thin, drawn in thirds of the innings count
no_career        players with no batting or bowling innings at all (the empty answer)
matches          matches of every format and gender, drawn evenly
super_over_matches  matches with a Super Over innings
ball_targets     (match_id, innings) pairs that really have balls, chosen from the balls table; the ball-page requests use only these, so a valid soak never asks for a missing resource
pairs            batter and bowler pairs that really met, from the most to the fewest meetings
Run on the Mac against the exact folder that is released. The file holds ids only.
"""
import argparse
import json
import random
from pathlib import Path

import duckdb


def build(folder: Path, seed: int = 1) -> dict:
    r = random.Random(seed)
    con = duckdb.connect()
    t = lambda name: f"read_parquet('{folder / (name + '.parquet')}')"  # noqa: E731
    careers = con.execute(f"""SELECT player_id, count(*) AS n FROM {t('player_batting_innings')} GROUP BY player_id
                              UNION ALL SELECT player_id, count(*) FROM {t('player_bowling_spells')} GROUP BY player_id""").fetchall()
    size: dict = {}
    for pid, n in careers:
        size[pid] = size.get(pid, 0) + n
    ranked = sorted(size, key=lambda p: -size[p])
    third = max(1, len(ranked) // 3)
    players = [{"id": p, "innings": size[p]} for chunk in (ranked[:third], ranked[third:2 * third], ranked[2 * third:]) for p in r.sample(chunk, min(60, len(chunk)))]
    everyone = [x[0] for x in con.execute(f"SELECT player_id FROM {t('players')}").fetchall()]
    none = [p for p in everyone if p not in size]
    matches = [{"id": str(m), "format": f, "gender": g} for m, f, g in con.execute(f"SELECT match_id, format, gender FROM {t('matches')}").fetchall()]
    by_kind: dict = {}
    for m in matches:
        by_kind.setdefault((m["format"], m["gender"]), []).append(m)
    chosen = [m for group in by_kind.values() for m in r.sample(group, min(40, len(group)))]
    supers = [{"id": str(m)} for (m,) in con.execute(f"SELECT DISTINCT match_id FROM {t('innings')} WHERE is_super_over").fetchall()]
    ids = {m["id"] for m in chosen}
    targets = [[str(m), int(i)] for m, i in con.execute(f"SELECT DISTINCT match_id, innings FROM {t('balls')} ORDER BY match_id, innings").fetchall() if str(m) in ids]
    pairs = con.execute(f"""SELECT striker_id, bowler_id FROM {t('balls')} WHERE striker_id IS NOT NULL AND bowler_id IS NOT NULL
                            GROUP BY striker_id, bowler_id HAVING count(*) >= 12 ORDER BY hash(striker_id || bowler_id || '{seed}') LIMIT 400""").fetchall()
    return {"players": players, "no_career": [{"id": p} for p in r.sample(none, min(40, len(none)))], "matches": chosen, "super_over_matches": r.sample(supers, min(40, len(supers))), "ball_targets": targets, "pairs": [list(p) for p in pairs]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    w = build(Path(a.data), a.seed)
    Path(a.out).write_text(json.dumps(w))
    print({k: len(v) for k, v in w.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
