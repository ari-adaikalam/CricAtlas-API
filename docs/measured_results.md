# Measured results

What was measured on the production server, and what was not. Every test ran on the one server the API runs on: 2 vCPU, 3.8 GiB of memory and no swap, shared with the proxy, the monitoring stack, the notifier and the host's own services. The workloads were synthetic, not real users. None of this measures maximum capacity.

The load went straight to the API's unix socket, so Cloudflare, Caddy's TLS and the network path were not part of it. Percentiles (p99) are taken per phase, 10 minutes in the long runs, and not pooled.

## Results

| Test | When | What ran | Result |
|---|---|---|---|
| Bounded load | 2026-10-13 | A mixed workload (60 percent light routes, 30 medium, 10 heavy), up to 12 requests a second for 20 minutes | No refusals. p99 about 100 ms for light routes, 150 ms medium, 285 ms heavy. The test stopped at 12 a second, so the limit above that is not known. API memory peaked at 249 MiB, and host available memory never fell under 2,620 MiB. |
| Full stack check | 2026-10-14 | 20 minutes at 12 a second with monitoring running | 14,094 requests, all answered 200. p99 181.8 ms light, 148.1 medium, 297.3 heavy. API memory peaked at 232 MiB, and host available memory fell no lower than 2,573 MiB. |
| 48 hour validation | 2026-10-14 to 16 | 12 callers with a think time of 3.5 s, sending 70 percent light, 27 medium and 3 heavy requests | 586,357 requests over 48.17 hours (3.38 a second), all answered 200. The median p99 was 124.9 ms for light routes, 107.4 medium, 240.1 heavy. The worst phase was 1,301.5 ms, on a leaderboard. No out-of-memory kill and no container restart. |
| Overload | 2026-10-13 | 400 callers that never back off sent about 1,075 requests a second for 60 s, with `/health` asked through the proxy every 0.25 s | 129 of 149 health probes failed, almost all with a 503 from the server's own connection limit of about 100. No restart, and memory unchanged. Normal answers came back 2.4 s after the load stopped. |

## Memory over 48 hours

The API's anonymous memory rose from 167.04 MiB (mean of hours 2 to 4) to 188.67 MiB (mean of the last 4 hours). The growth slowed through the run, by 12 hour block: 1.93, 0.88, 0.43 and 0.16 MiB an hour. The maximum was 233 MiB, and in the hour after the traffic stopped it fell from about 188 to 179 MiB.

The cause of the growth is not established. Allocator retention explained most of it in short tests, and that is not shown for the 48 hour run. This describes behaviour at that workload for 48 hours. It says nothing about higher load, longer periods or later releases.

## What filled first

In the bounded tests the two heavy request slots and the CPU filled before memory did. A leaderboard costs 20 to 40 ms of CPU, so a steady flood of them fills the slots. A request then waits briefly (1 second, 4 places by default) and is refused with `Retry-After` when the wait ends or the room is full. Each client also has one heavy request at a time and 60 tokens a minute, which are policy and not hardware.

## Other checks

- **Grafana memory:** Grafana was killed for running out of memory on 2026-10-13, at 455 MiB in a 512 MiB limit. With the settings that followed, 15 minutes of use under 6 requests a second peaked at 420 MiB, with no kill and no restart.
- **Cold boot:** after a reboot, the public `/health` answered about 25 seconds later with no manual step, and the containers were healthy within 30 seconds.
- **Daily data refresh:** a full preparation from the Cricsheet archives took 3 h 46 min under the job's limits (50 percent of a CPU, 1,000 MiB). When no match has changed, the job reuses its kept results and takes about 26 minutes (measured 2026-10-30 and 31). Its export step peaked at 709 to 747 MiB of anonymous memory.

## Not measured

Capacity above 12 requests a second. Behaviour beyond 48 hours. A flood from many addresses. The network path through Cloudflare and Caddy's TLS under load. Real user traffic. The API under load while the daily refresh runs its export.

## Evidence

The raw evidence (run folders, reports and checksums) is kept privately and is not part of this repository. The load, probe and soak tools are in `loadtest/vps/`, and `validation_report.py` regenerates the final validation report from its run folder.
