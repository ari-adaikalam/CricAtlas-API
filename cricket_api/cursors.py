"""Pagination cursors. Opaque, signed, and tied to one data version and one route, so a cursor cannot be forged or used against newer data or another route.

A valid signature only says that this server made the cursor. It is not a reason to trust what is inside it, so every cursor is checked before it is used: the envelope (exactly the three keys it
is made with, of the right types) and then the key values against the shape the route expects (how many, which types, and bounds). A cursor that fails any check is the same plain "not valid" answer,
and the answer never repeats what was in it. Nothing here has a switch to skip the checks.
"""
import base64
import hashlib
import hmac
import json
import re
from datetime import date

INVALID = "the cursor is not valid"
ENVELOPE = {"v", "r", "k"}
DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


class Int:
    """A whole number between lo and hi. A boolean is not a number here, though Python counts it as one."""
    def __init__(self, lo: int, hi: int):
        self.lo, self.hi = lo, hi

    def check(self, value):
        if type(value) is not int or not self.lo <= value <= self.hi:
            raise ValueError(INVALID)
        return value


class Date:
    """A calendar date written YYYY-MM-DD, nothing looser (Python's own parser also takes other forms). The value handed back is a date."""
    def check(self, value):
        if type(value) is not str or not DATE_TEXT.fullmatch(value):
            raise ValueError(INVALID)
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(INVALID) from None


class Text:
    """A string of at most `longest` characters."""
    def __init__(self, longest: int):
        self.longest = longest

    def check(self, value):
        if type(value) is not str or len(value) > self.longest:
            raise ValueError(INVALID)
        return value


class Nullable:
    """The inner kind of value, or None. A split's key is None for the group of matches that have no value for the dimension."""
    def __init__(self, inner):
        self.inner = inner

    def check(self, value):
        return None if value is None else self.inner.check(value)


MATCH_ID = Int(1, 2_147_483_647)                 # as the match id in a path
DELIVERY = Int(0, 10_000_000)
COUNT = Int(0, 1_000_000_000)
NAME = Text(300)

# The key of each cursor, by what the route pages over.
DATE_AND_MATCH = (Date(), MATCH_ID)              # /v1/matches and a player's match history: start date, match id
DELIVERY_NUMBER = (DELIVERY,)                    # the balls of an innings
COUNT_AND_KEY = (COUNT, Nullable(NAME))          # splits of a player or a team: matches counted, then the group key


def _sign(secret: str, body: bytes) -> bytes:
    return hmac.new(secret.encode(), body, hashlib.sha256).digest()[:16]


def encode(secret: str, data_version: str, route: str, values: list) -> str:
    body = json.dumps({"v": data_version, "r": route, "k": values}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(_sign(secret, body) + body).decode().rstrip("=")


def decode(secret: str, data_version: str, route: str, cursor: str, shape: tuple) -> list:
    """The key values, checked against `shape` and converted (a date becomes a date), or ValueError with a plain reason. A cursor from another route or data version is refused."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        signature, body = raw[:16], raw[16:]
        if not hmac.compare_digest(signature, _sign(secret, body)):
            raise ValueError(INVALID)
        payload = json.loads(body)
    except (ValueError, TypeError, RecursionError):                 # json.JSONDecodeError is a ValueError. Deeply nested input is a RecursionError
        raise ValueError(INVALID) from None
    if type(payload) is not dict or set(payload) != ENVELOPE or type(payload["v"]) is not str or type(payload["r"]) is not str or type(payload["k"]) is not list:
        raise ValueError(INVALID)
    if payload["v"] != data_version:
        raise ValueError("the cursor belongs to an older version of the data, start again without it")
    if payload["r"] != route:
        raise ValueError("the cursor belongs to another route")
    key = payload["k"]
    if len(key) != len(shape):
        raise ValueError(INVALID)
    return [kind.check(value) for kind, value in zip(shape, key)]
