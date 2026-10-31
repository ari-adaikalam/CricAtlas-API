"""A token bucket per client, in process memory, with a bounded table.

One worker process means one limiter, which is why the server runs a single worker. Heavy routes cost more tokens than light ones.

Memory is bounded without ever freeing a client that still owes tokens:

  * A bucket that has refilled to full is the same as no bucket, so it is dropped. Only clients that spent tokens recently are kept, and each one is kept for at most burst / rate seconds.
  * If the table is full of such clients, a client the table has not seen is not given a bucket of its own. All unseen clients then share one overflow bucket, which has the allowance of a
    single client. Clients already in the table keep their own buckets and nobody is reset. A flood of new keys therefore slows only itself and the other unseen clients, and normal
    service comes back as soon as entries refill (at most burst / rate seconds).
"""
import heapq
import itertools
import math
import threading
import time
from dataclasses import dataclass


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after: int


class RateLimiter:
    def __init__(self, per_minute: int, burst: int, max_clients: int = 10_000, clock=time.monotonic):
        self.per_minute, self.burst, self.max_clients, self.clock = per_minute, burst, max_clients, clock
        self.rate = per_minute / 60.0
        self.capacity = float(max(burst, 1))
        self._buckets: dict[str, list] = {}                    # client -> [tokens, stamp, serial, full_at]
        self._due: list[tuple[float, int, str]] = []           # (full_at, serial, client), a client's older entries are skipped by serial
        self._serial = itertools.count()
        self._overflow = [self.capacity, None]                 # [tokens, stamp], shared by clients the full table has not seen
        self.overflowed = 0
        self._lock = threading.Lock()

    def take(self, client: str, cost: int = 1) -> Decision:
        now = self.clock()
        with self._lock:
            self._drop_refilled(now)
            bucket = self._buckets.get(client)
            if bucket is None and len(self._buckets) >= self.max_clients:
                self.overflowed += 1
                tokens, stamp = self._overflow[0], self._overflow[1] if self._overflow[1] is not None else now
                tokens, allowed = self._spend(tokens, stamp, now, cost)
                self._overflow = [tokens, now]
                return self._decision(allowed, tokens, cost)
            tokens, stamp = (bucket[0], bucket[1]) if bucket else (self.capacity, now)
            tokens, allowed = self._spend(tokens, stamp, now, cost)
            self._keep(client, tokens, now)
            return self._decision(allowed, tokens, cost)

    def _spend(self, tokens: float, stamp: float, now: float, cost: int) -> tuple[float, bool]:
        tokens = min(self.capacity, tokens + (now - stamp) * self.rate)
        allowed = tokens >= cost
        return (tokens - cost if allowed else tokens), allowed

    def _decision(self, allowed: bool, tokens: float, cost: int) -> Decision:
        missing = max(0.0, cost - tokens)
        return Decision(allowed, self.per_minute, int(tokens), int((self.capacity - tokens) / self.rate) + 1 if self.rate else 0,
                        int(missing / self.rate) + 1 if self.rate and not allowed else 0)

    def _keep(self, client: str, tokens: float, now: float) -> None:
        """Store the bucket, or forget it when it is full, since a full bucket says nothing a new one would not."""
        if tokens >= self.capacity:
            self._buckets.pop(client, None)
            return
        serial = next(self._serial)
        full_at = now + (self.capacity - tokens) / self.rate if self.rate else math.inf
        self._buckets[client] = [tokens, now, serial, full_at]
        if full_at != math.inf:
            heapq.heappush(self._due, (full_at, serial, client))
            if len(self._due) > 4 * len(self._buckets) + 64:    # stale entries pile up when one client sends many requests, so rebuild from the live ones
                self._due = [(b[3], b[2], key) for key, b in self._buckets.items() if b[3] != math.inf]
                heapq.heapify(self._due)

    def _drop_refilled(self, now: float) -> None:
        while self._due and self._due[0][0] <= now:
            _, serial, client = heapq.heappop(self._due)
            bucket = self._buckets.get(client)
            if bucket is not None and bucket[2] == serial:
                del self._buckets[client]

    def clients(self) -> int:
        return len(self._buckets)
