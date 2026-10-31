"""Settings for the server, read from the environment so nothing is baked into the image."""
import math
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """A setting is not usable. The server refuses to start, and the launcher prints the message and exits with 2."""


def _int(name: str, default: int) -> int:
    text = os.environ.get(name, default)
    try:
        return int(text)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, not {str(text)[:30]!r}") from None


MIN_SECRET_CHARS = 32

HSTS_DEFAULT = 31_536_000          # one year, what every response has always carried
HSTS_MAX = 63_072_000              # two years: longer than that is not a value anyone has a reason to send
HSTS_FORMAT = re.compile(r"0|[1-9][0-9]{0,9}")           # ASCII digits, no sign, no leading zero, so "007" and "+5" and "1_000" are refused


def parse_hsts_max_age(text: object, name: str = "CRICKET_API_HSTS_MAX_AGE") -> int:
    """The HSTS max-age in seconds, from 0 to two years. 0 is allowed on purpose: it asks a browser to remove the policy it stored for this host name, which is how HSTS is switched off again (it works only after a valid HTTPS response reaches that browser, and it cannot override a parent domain's policy or a preload list). The message repeats at most a
    short piece of what was given."""
    shown = text[:30] if isinstance(text, str) else f"a value of type {type(text).__name__}"
    rule = f"{name} must be a whole number of seconds from 0 to {HSTS_MAX} (0 switches HSTS off for this host name and asks browsers to remove the policy they stored for it)"
    if not isinstance(text, str) or len(text) > 40 or HSTS_FORMAT.fullmatch(text) is None:
        raise ConfigError(f"{rule}, not {shown!r}" if isinstance(text, str) else f"{rule}, not {shown}")
    value = int(text)
    if value > HSTS_MAX:
        raise ConfigError(f"{rule}, not {shown!r}")
    return value


def _hsts_max_age() -> int:
    text = os.environ.get("CRICKET_API_HSTS_MAX_AGE")
    return HSTS_DEFAULT if text is None else parse_hsts_max_age(text)


def _cursor_secret() -> str:
    """The signing secret. A configured one is used as it is, even when it is empty, so that a short or empty value is refused (see Settings) and never replaced behind the operator's back. Without
    one, a random one is made with the operating system's secure generator (256 bits), and lasts until the process restarts."""
    if "CRICKET_API_SECRET" in os.environ:
        return os.environ["CRICKET_API_SECRET"]
    return secrets.token_urlsafe(32)


def _float(name: str, default: float) -> float:
    text = os.environ.get(name, default)
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{name} must be a number, not {str(text)[:30]!r}") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number, not {str(text)[:30]!r}")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CRICKET_API_DATA", "api/data/exports/current")))
    refresh_status_file: Path | None = field(default_factory=lambda: Path(os.environ["CRICKET_API_REFRESH_STATUS"]) if os.environ.get("CRICKET_API_REFRESH_STATUS") else None)
    memory_limit: str = field(default_factory=lambda: os.environ.get("CRICKET_API_MEMORY", "1GB"))
    threads: int = field(default_factory=lambda: _int("CRICKET_API_THREADS", 2))
    query_seconds: float = field(default_factory=lambda: _float("CRICKET_API_QUERY_SECONDS", 5))
    request_seconds: float = field(default_factory=lambda: _float("CRICKET_API_REQUEST_SECONDS", 10))     # the whole request, all its queries together, see budget.py
    max_concurrent_queries: int = field(default_factory=lambda: _int("CRICKET_API_CONCURRENCY", 4))
    queue_seconds: float = field(default_factory=lambda: _float("CRICKET_API_QUEUE_SECONDS", 1.5))      # how long a request waits for a free query slot
    # Where the query engine may spill to disk and how much, see scratch.py. Unset, the engine's own folder is used, and the cap still applies.
    scratch_dir: str | None = field(default_factory=lambda: os.environ.get("CRICKET_API_SCRATCH"))
    scratch_max: str = field(default_factory=lambda: os.environ.get("CRICKET_API_SCRATCH_MAX", "1GiB"))
    default_page: int = 50
    max_page: int = 500
    max_ball_page: int = 1000
    # Cursors are signed with this. Without a configured secret a random one is made at start, so cursors then last only until a restart.
    cursor_secret: str = field(default_factory=lambda: _cursor_secret(), repr=False)         # never in repr: a settings object is printed and logged
    rate_per_minute: int = field(default_factory=lambda: _int("CRICKET_API_RATE", 60))
    rate_burst: int = field(default_factory=lambda: _int("CRICKET_API_BURST", 20))
    # Admission, see admission.py: how many requests wait for the database in each class and for how long, and how many connections the launcher lets the server hold (0 derives it).
    heavy_room: int = field(default_factory=lambda: _int("CRICKET_API_HEAVY_ROOM", 4))
    medium_room: int = field(default_factory=lambda: _int("CRICKET_API_MEDIUM_ROOM", 8))
    light_room: int = field(default_factory=lambda: _int("CRICKET_API_LIGHT_ROOM", 32))
    heavy_wait: float = field(default_factory=lambda: _float("CRICKET_API_HEAVY_WAIT", 1.0))
    medium_wait: float = field(default_factory=lambda: _float("CRICKET_API_MEDIUM_WAIT", 1.5))
    light_wait: float = field(default_factory=lambda: _float("CRICKET_API_LIGHT_WAIT", 1.5))
    max_connections: int = field(default_factory=lambda: _int("CRICKET_API_MAX_CONNECTIONS", 0))
    # Who a request comes from, see clients.py. The default ignores every forwarding header.
    client_ip: str = field(default_factory=lambda: os.environ.get("CRICKET_API_CLIENT_IP", "peer"))
    trusted_proxies: str = field(default_factory=lambda: os.environ.get("CRICKET_API_TRUSTED_PROXIES", ""))
    client_ip_header: str = field(default_factory=lambda: os.environ.get("CRICKET_API_CLIENT_IP_HEADER", "X-Forwarded-For"))
    ipv6_prefix: int = field(default_factory=lambda: _int("CRICKET_API_IPV6_PREFIX", 64))
    max_clients: int = field(default_factory=lambda: _int("CRICKET_API_MAX_CLIENTS", 10_000))
    # A shared cache (a CDN) stores cacheable answers. Then those answers carry nothing that belongs to one client, see caching.py.
    shared_cache: bool = field(default_factory=lambda: os.environ.get("CRICKET_API_SHARED_CACHE", "").lower() in ("1", "true", "yes"))
    # The Strict-Transport-Security max-age in seconds, 0 to two years. includeSubDomains is always sent and preload never is, see the README.
    hsts_max_age: int = field(default_factory=_hsts_max_age)
    verify_files: bool = True
    docs_enabled: bool = field(default_factory=lambda: os.environ.get("CRICKET_API_DOCS", "1").lower() not in ("0", "false", "no"))
    # The public address of this API as an origin (https://host), so the documentation page can name its share image with an absolute URL. Empty: the page carries no share tags. The repository holds no
    # installation's name, so it comes from the environment.
    public_url: str = field(default_factory=lambda: os.environ.get("CRICKET_API_PUBLIC_URL", ""))
    # The origin of the web app that serves this API under its own /api (https://host). Only the web variant of the documentation (/_via/web/...) uses it, for its absolute share URLs and its server address.
    web_origin: str = field(default_factory=lambda: os.environ.get("CRICKET_API_WEB_ORIGIN", ""))

    def __post_init__(self):
        if not isinstance(self.web_origin, str) or (self.web_origin and not re.fullmatch(r"https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:\d{1,5})?", self.web_origin)):
            raise ConfigError("CRICKET_API_WEB_ORIGIN must be empty or an https origin such as https://host, with no path and no trailing slash")
        if not isinstance(self.public_url, str) or (self.public_url and not re.fullmatch(r"https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:\d{1,5})?", self.public_url)):
            raise ConfigError("CRICKET_API_PUBLIC_URL must be empty or an https origin such as https://host, with no path and no trailing slash")
        if type(self.hsts_max_age) is not int or not 0 <= self.hsts_max_age <= HSTS_MAX:           # a value given in code is held to the same rule as one from the environment (a bool is not a number of seconds)
            raise ConfigError(f"CRICKET_API_HSTS_MAX_AGE must be a whole number of seconds from 0 to {HSTS_MAX}")
        # Applies to every way of giving the secret, the environment and a value passed in code alike. The message never holds the value or its length.
        if not isinstance(self.cursor_secret, str) or len(self.cursor_secret) < MIN_SECRET_CHARS:
            raise ConfigError(f"CRICKET_API_SECRET is empty or too short: it must be at least {MIN_SECRET_CHARS} characters, and random. Leave it unset for a random one that lasts until a restart, "
                              "or make one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'")
