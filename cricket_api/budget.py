"""One time budget for a whole request, and a way to say the client has left (audit C2).

Each query already has its own time limit, but a request that runs several queries could spend that limit several times. The admission middleware opens a budget for every admitted request; the data layer
reads it before and during each query: a query is never given more time than the request has left, and none starts once the budget is spent or the client has gone. The handler runs on a worker thread
that cannot be stopped from outside, so this is how the work after the deadline is not started. The admission unit is held until that thread ends (admission.AdmissionMiddleware), so the limits still hold.

The budget travels in a context variable. The framework copies the context into the worker thread, so the query code sees the budget of the request it is serving, and a request with no budget (a health check,
a script, a test that calls the database directly) is unlimited as before.
"""
import contextvars
import threading
import time
from dataclasses import dataclass, field

CURRENT: contextvars.ContextVar["Budget | None"] = contextvars.ContextVar("cricket_request_budget", default=None)


@dataclass
class Budget:
    seconds: float
    started: float = field(default_factory=time.monotonic)
    gone: threading.Event = field(default_factory=threading.Event)

    def left(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    def spent(self) -> bool:
        return self.left() <= 0

    def abandon(self) -> None:
        """The client has left or the response is no longer wanted: queries not yet started do not run."""
        self.gone.set()


def current() -> "Budget | None":
    return CURRENT.get()
