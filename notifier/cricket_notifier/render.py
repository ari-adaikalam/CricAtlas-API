"""Turning a kind and a context into a message: a subject, an HTML body and a text body, from template files that are kept apart from everything else.

The templates (the `templates` folder: a base layout and, for each kind, a subject, a text and an HTML file) can be redesigned without touching scheduling, delivery, budgets or the ledger. The engine is
Jinja in its sandbox, HTML is escaped and text is not, and a variable a template needs that the context does not have is an error and not an empty string.

A rendered message has a size limit by kind (see payload.py). If the full message is over it, a shorter one is rendered from the same context with its lists cut and its strings shortened, and if that is still
over, a fixed minimal message is sent in its place. A message is therefore always within its limit, and the ledger refuses one that is not.
"""
import copy
import re
from dataclasses import dataclass
from pathlib import Path

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from cricket_notifier import payload
from cricket_notifier.config import Config
from cricket_notifier.errors import PayloadTooLarge

TEMPLATES = Path(__file__).parent / "templates"
KINDS = ("summary", "alert", "recovery", "digest", "suppression", "email_path", "host", "refresh", "minimal")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
MARKS = {"incomplete": "incomplete, see the notes", "may_undercount": "may undercount, see the notes", "restart_unknown": "a restart cannot be ruled out", "no_data": "no data",
         "highest_observed": "highest observed"}
FOOTER = "A private monitoring message from the CricAtlas API notifier."
SHRUNK_LIST = 3
SHRUNK_TEXT = 80


@dataclass(frozen=True)
class Rendered:
    subject: str
    html: str
    text: str


def mark_words(marks) -> str:
    words = [MARKS.get(m, "") for m in (marks or []) if m in MARKS and m != "highest_observed"]
    return f" [{'; '.join(w for w in words if w)}]" if words else ""


def shrink(node):
    """A shorter copy of a context: every list cut to a few items, every long string shortened. The keys are kept, so a template that renders the full context renders this."""
    if isinstance(node, dict):
        return {k: shrink(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [shrink(v) for v in node[:SHRUNK_LIST]]
    if isinstance(node, str) and len(node) > SHRUNK_TEXT:
        return node[:SHRUNK_TEXT]
    return node


class Renderer:
    def __init__(self, config: Config, templates_dir: str | Path | None = None):
        self.config = config
        self.dir = Path(templates_dir) if templates_dir else TEMPLATES
        self.env = SandboxedEnvironment(loader=jinja2.FileSystemLoader(str(self.dir)), autoescape=jinja2.select_autoescape(enabled_extensions=("html",), default=False),
                                        undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        self.env.filters["marks"] = mark_words
        self.counters = {"shrunk": 0, "minimal": 0}

    def render(self, kind: str, context: dict) -> Rendered:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        data = {**context, "prefix": self.config.email.subject_prefix, "footer": FOOTER}
        data.setdefault("shrunk", False)
        subject = self.env.get_template(f"{kind}.subject.txt").render(data)
        subject = CONTROL.sub(" ", subject).strip()[:150]
        data["title"] = subject                                                                # the heading of the message is its subject
        return Rendered(subject, self.env.get_template(f"{kind}.html").render(data), self.env.get_template(f"{kind}.txt").render(data))

    def message(self, kind: str, context: dict) -> tuple[Rendered, bytes]:
        """The rendered message and the exact bytes to store and send, always within the size limit for the kind."""
        e = self.config.email
        attempts = ((kind, context), (kind, {**shrink(context), "shrunk": True}), ("minimal", {"original_kind": kind, "title": f"{kind} message", "shrunk": True}))
        for index, (use_kind, data) in enumerate(attempts):
            rendered = self.render(use_kind, copy.deepcopy(data))
            body = payload.build(e.sender, e.recipient, rendered.subject, rendered.html, rendered.text)
            try:
                payload.check_size(kind, body)
            except PayloadTooLarge:
                continue
            if index:
                self.counters["shrunk" if index == 1 else "minimal"] += 1
            return rendered, body
        raise PayloadTooLarge(f"even the minimal {kind} message is over its limit")      # cannot happen with the fixed minimal templates
