"""The request body of a message: built once, as exact bytes, and never rebuilt.

Resend returns the original result for a repeated Idempotency-Key only when the body is the same, byte for byte, and answers 409 when it is not. So the body is built once, stored in the ledger with
its SHA-256, and every attempt sends those stored bytes. The body is canonical JSON (UTF-8, sorted keys, fixed separators). It is sanitized by construction: the renderer escapes what it is given,
and nothing in it is a secret. Its size is limited by kind, and a body over its limit is refused before anything is stored.
"""
import hashlib
import json

from cricket_notifier.errors import PayloadTooLarge

SUMMARY_MAX_BYTES = 64 * 1024
OTHER_MAX_BYTES = 32 * 1024
SUMMARY_KINDS = ("summary",)
KINDS = ("alert", "recovery", "summary", "digest", "suppression", "email_path", "host", "refresh")
NOTICE_KINDS = ("suppression", "email_path", "host", "refresh")
REGULAR_KINDS = ("alert", "recovery", "digest")


def limit_for(kind: str) -> int:
    return SUMMARY_MAX_BYTES if kind in SUMMARY_KINDS else OTHER_MAX_BYTES


def build(sender: str, recipient: str, subject: str, html: str, text: str) -> bytes:
    """The exact bytes that are sent."""
    return json.dumps({"from": sender, "to": [recipient], "subject": subject, "html": html, "text": text}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def check_size(kind: str, body: bytes) -> bytes:
    """The body, if it is within its limit. Raises PayloadTooLarge otherwise, so an oversized message is never stored."""
    if len(body) > limit_for(kind):
        raise PayloadTooLarge(f"a {kind} message is {len(body)} bytes, and the limit is {limit_for(kind)}")
    return body


def idempotency_key(key: str) -> str:
    """The Idempotency-Key for a ledger key: the key itself when it is short and plain, else a hash of it. Resend allows 1 to 256 characters."""
    if len(key) <= 200 and all(32 < ord(c) < 127 for c in key):
        return key
    return "h/" + hashlib.sha256(key.encode("utf-8")).hexdigest()
