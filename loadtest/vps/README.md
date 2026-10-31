# VPS tests: spill, scratch exhaustion, load and soak

These are the scripts that produced the first VPS measurements recorded in `docs/measured_results.md` ("First VPS test, 2026-10-11"). They run on the VPS as root, against the real release, and every request goes to the API over
its unix socket, so Cloudflare and its cache are never involved. They are kept as they were run: paths such as `/tmp` and `/home/ubuntu` are the VPS's.

| Script | What it does |
|---|---|
| `spill_rig_setup.sh`, `spill_rig_start.sh` | An isolated test rig: a 256 MB loop volume for scratch and a throwaway API container from the live image, hardened like production, with no network, the data read-only, and only its own DuckDB memory and scratch cap lowered. The public container's limits are never changed. |
| `spill_check.sh` | Forces a heavy route to spill in the rig, checks the answer is identical to the live API's, the scratch is used and then empty, the container does not restart, and writes to the code and data mounts fail. |
| `exhaustion_check.sh` | Two cases: the spill reaches its cap, and the volume itself fills. Checks the clean 5xx, `/health` and light routes during it, no restart, and recovery after the space is freed. |
| `spill_rig_teardown.sh` | Removes the container, the mount and the image file. |
| `load_ladder.sh`, `socket_load.py` | Load against the live API container from a separate tester container: many distinct callers (each stays under its own rate limit), then a heavy flood and an overload of callers that never back off. A guard samples every 2 seconds and kills the load on an OOM, host memory under 600 MiB, API memory over 2090 MiB, root disk under 8 GB or memory pressure over 10 percent for 60 seconds. |
| `soak_start.sh`, `soak_sampler.sh` | A 25-hour soak of modest mixed traffic and a one-line-a-minute sampler of the API container's memory, events and the host. |

`socket_get.py` is the one-request client used inside the rig. Run the scripts in the order of the table and read the stop conditions before the load test.

## Tools added after the soak (2026-10-12 and 2026-10-13)

These run on the server as root from a tools folder (`/home/ubuntu/memcycles-tools/`), every request goes over the API's socket, and each is tested in `api/tests` or `api/deploy/tests`.

| Script | What it does |
|---|---|
| `analyze_soak.py`, `soak_after.sh`, `soak_supervisor.py` | Judge a soak from its raw files with exact windows; watch the API after the traffic stops; supervise a run (thresholds, stops the traffic, watches the portal and `tailscaled` when they exist). |
| `capacity_baseline.sh`, `capacity_report.py`, `capacity_probe.py`, `capacity_guard.py`, `capacity_targets.json`, `socket_rate.py` | The offered-rate ladder and the bounded confirmation, with the recorded targets, the guard and the report. |
| `memory_cycles.sh`, `memory_cycles_report.py` | Repeated load and idle cycles with a pre-fixed trend rule (Part A of the diagnostic window). |
| `part_b.sh`, `retest.sh` | The wrappers used for Part B and the leaderboard retest (kept as run). |
| `gate1_run.sh`, `retest_memory.py` | The Gate 1 measurement run under the protocol recorded in `docs/measured_results.md` ("Criteria of record"): replay gate, warm-up until the memory levels off, ladder, confirmation, idle, memory verdict. |
| `record_replay.py` | Records the answers of a fixed request set from one version of the code, compares two recordings, and replays one against the live socket. |
| `bounded_run.sh` | Runs any probe with a deadline the host enforces and removes what it started. Every bounded probe from now on goes through it. |
| `validation_run.sh` | Starts the final 48 to 72 hour validation run (traffic, sampler, supervisor, and a 72-hour ceiling timer that is only a backstop: its deadline was seen to shift, see runbook section 10) in a new folder. |


## Other tools in this folder

These were used in Gate 1, Gate 2 and the validation run, and the evidence they wrote is archived (see `api/docs/internal/README.md`, "Evidence"). They are listed here so that no file is a mystery.

| File | What it does |
|---|---|
| `build_workload.py` | Builds the workload file of the `final` mix for `socket_load.py` from an export folder, so a run asks for the shapes that matter and not only the first ids a search returns. |
| `burst_check.py` | A short same-client burst check of the token costs (audit finding B1) against the API's unix socket. |
| `caddy_probe.py` | Asks `/health` through the real Caddy path. |
| `flood_check.sh`, `flood_report.py`, `make_probe_cert.sh` | A bounded overload of the deployed API with `/health` asked through Caddy the whole time, the judgement of its folder, and the short-lived client certificate the check needs (signed by the origin CA that stays on the Mac). |
| `gate1_extras.sh` | The three remaining Gate 1 checks in one bounded run, stopping at the first failure. |
| `final_mix_gate.py` | Decides from a `capacity_baseline.sh --mix final --confirm` folder whether the final-mix run counts as passed and whether anything may follow it. |
| `container_monitor.sh` | One container's memory and responsiveness every few seconds for a bounded test. Read only. |
| `grafana_monitor.sh` | Grafana's memory every few seconds while the dashboard was opened, burst-tested and the host rebooted in Gate 2 (it wrote `grafana-monitor.csv` in the Gate 2 evidence). Read only. |
| `disk_sampler.sh` | Records the disk budget lines every 10 minutes into `disk.csv` for the validation report. Read only. |
| `validation_finalize.sh` | The end of a validation run, done by the VPS itself (described below). |
| `cricket-soak-supervisor.service` | The systemd unit that ran `soak_supervisor.py` for the first soak. It has no arguments, so it takes the defaults: state folder `/home/ubuntu/soak`, traffic container `cricket-soak-traffic` and a 25 hour duration. It is historical and must not be enabled to "restore" anything. To repeat a soak, use the current `soak_supervisor.py` from the repository (both archived VPS copies differ from it), restore only the files the run needs, check the arguments and that the traffic container exists, and start it by hand. The unit is disabled on the host and its file stays there only as a record. |

## The validation report

After the 48 to 72 hour run, `validation_report.py` turns the run folder into a redacted statistics report for the public repository. The end of the run is collected by the VPS itself (the unit `cricket-validation-finalizer`, started by `validation_run.sh`), so it happens whether or not anyone is connected:

1. it waits for `cricket-validation-traffic` to stop (completion, a safety trip or the ceiling timer) and for the supervisor's verdict file;
2. `traffic-times.json`: the traffic container's actual start and end (the report's windows start and end there, and samples after the end are kept apart as idle samples);
3. `traffic-phases.jsonl`: the phase records from the container's log;
4. `restarts-end.txt`: the end `mem_report.sh` reading (the start reading was taken by `validation_run.sh`);
5. `after-traffic.csv`: 60 minutes of read-only idle observation with `soak_after.sh --traffic cricket-validation-traffic`;
6. `report-draft.md` and `.json`, judged strictly, and the two sampler units stopped; `FINALIZED.txt` and `finalize.log` say what happened. A failed step is logged and the next one still runs.

Grafana visits are written by hand to `grafana-visits.csv` (`start_utc,end_utc,note`); the report lists them and flags any over 15 minutes. The final report:

```
python3 validation_report.py --dir VALIDATION_DIR --out OUT_PREFIX --release RELEASE_ID --strict
```

`--strict` makes the exit code 2 (after the report is written) when any input was not clean: missing, corrupt or short traffic phases, no recorded traffic start and end, malformed or gapped samples, missing restart or recovery readings, a Grafana visit over 15 minutes. It writes `OUT_PREFIX.md` and `OUT_PREFIX.json`: the workload, throughput by hour, success and latency per route family and class, outcome counts, API memory (hours 2 to 4 against the last 4 hours of the traffic, the trend overall and from hour 8), CPU, disk growth by log class (`disk.csv`, every 10 minutes), OOM kills, failed health minutes, the supervisor's verdict, restarts (the difference between the start and end `mem_report.sh` readings), the idle samples and the idle observation, and the limitations. A missing health reading is never counted as a recovery. The report records the SHA-256 of every input, so the same files give the same report, and it refuses to write a report that contains something shaped like an address, a path, an e-mail address, a tailnet name or a token. Phase percentiles are not pooled, and the report says so. It shows stability at the tested workload, not maximum capacity.
