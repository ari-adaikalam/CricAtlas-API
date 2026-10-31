"""The configuration, in one TOML file outside the repository.

It holds the recipient, the send time and zone, the sender, the budgets, and the retry, catch-up, storage and retention limits. Nothing in the scheduler or the sender names an address, a time or a
zone: changing any of them is an edit to this file and a restart. The file is checked completely at start. An unknown section or key, a wrong type, a value out of range or a combination that cannot
work (a reserve larger than its cap, a budget over what Resend allows, a zone that does not exist) stops the notifier with a message, and nothing falls back to a default for a value that was given.
Secrets are never in this file, only the paths of the files that hold them (see secrets.py).
"""
import ipaddress
import re
import tomllib
from dataclasses import dataclass, field, fields
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cricket_notifier.errors import ConfigError

RESEND_DAILY_LIMIT = 100        # the Free plan, the figures the budgets must stay under
RESEND_MONTHLY_LIMIT = 3000
ADDRESS = re.compile(r"[^@\s<>,;\"'()\[\]\\]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")
SENDER = re.compile(r"(?:[^<>\r\n\"]{1,60} <(?P<named>[^<>\s]+)>|(?P<bare>[^<>\s]+))")
TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


@dataclass(frozen=True)
class Email:
    recipient: str = ""
    sender: str = ""
    subject_prefix: str = "[CricAtlas API]"


@dataclass(frozen=True)
class Schedule:
    send_time: str = "21:00"
    timezone: str = "Australia/Sydney"
    grace_minutes: int = 30
    catch_up_hours: int = 48

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def clock(self) -> clock_time:
        hour, minute = TIME.fullmatch(self.send_time).groups()
        return clock_time(int(hour), int(minute))


@dataclass(frozen=True)
class Budgets:
    daily_cap: int = 40
    daily_summary_reserve: int = 3
    daily_last_slots: int = 2
    monthly_cap: int = 900
    monthly_summary_reserve: int = 40
    monthly_last_slots: int = 10
    hourly_burst: int = 6
    account_headroom_daily: int = 90
    account_headroom_monthly: int = 2700


@dataclass(frozen=True)
class Retry:
    max_attempts: int = 5
    backoff_seconds: tuple = (30, 120, 600, 1800)
    timeout_seconds: float = 10.0
    jitter: float = 0.2


@dataclass(frozen=True)
class Deadlines:
    summary_hours: float = 2.0
    alert_minutes: float = 30.0
    reminder_hours: float = 12.0


@dataclass(frozen=True)
class Breaker:
    failures_to_open: int = 3
    cooldown_initial_minutes: float = 30.0
    cooldown_max_hours: float = 6.0
    queue_max_rows: int = 200


@dataclass(frozen=True)
class Storage:
    ledger_path: str = ""
    volume_mb: int = 64
    max_db_mb: int = 40
    wal_target_mb: int = 8
    wal_refuse_mb: int = 16
    unfinished_payload_mb: int = 8
    summary_reserve_mb: int = 2
    finished_payload_days: int = 30
    finished_payload_mb: int = 10
    metadata_days: int = 90
    protected_days: int = 180
    protected_max_rows: int = 1000
    free_disk_floor_mb: int = 2               # measured on the ledger's own volume, which holds nothing else
    prune_at_percent: int = 80


@dataclass(frozen=True)
class Webhook:
    bind_host: str = "127.0.0.1"
    bind_port: int = 9095
    max_body_kb: int = 1024
    max_alerts: int = 100
    requests_per_minute: int = 120


@dataclass(frozen=True)
class Metrics:
    bind_host: str = "127.0.0.1"
    bind_port: int = 9096


@dataclass(frozen=True)
class Prometheus:
    url: str = "http://127.0.0.1:9090"
    scrape_interval_seconds: float = 15.0
    max_boundary_lag_intervals: float = 2.0
    max_samples: int = 200_000
    timeout_seconds: float = 20.0
    lookback_hours: float = 6.0          # how far before a period to look for the sample its start boundary uses
    api_job: str = "cricket-api"         # the job name of the API in Prometheus, for the process start time
    node_job: str = "node"
    bandwidth_device: str = "eth0"       # the public network interface whose transmitted bytes are kept by the hour


@dataclass(frozen=True)
class Disk:
    path: str = "/"
    critical_free_percent: int = 15
    recover_free_percent: int = 20
    check_seconds: int = 300


@dataclass(frozen=True)
class Refresh:
    results_dir: str = ""          # the folder of per-attempt refresh records (read only). Empty: no result emails.
    max_files: int = 64              # records read in one pass, newest first
    unfinished_minutes: int = 45     # a record that is still waiting for the publisher after this long is reported as an unfinished publication


@dataclass(frozen=True)
class Resend:
    api_base: str = "https://api.resend.com"


@dataclass(frozen=True)
class Secrets:
    resend_api_key_file: str = ""
    webhook_token_file: str = ""


@dataclass(frozen=True)
class Config:
    email: Email = field(default_factory=Email)
    schedule: Schedule = field(default_factory=Schedule)
    budgets: Budgets = field(default_factory=Budgets)
    retry: Retry = field(default_factory=Retry)
    deadlines: Deadlines = field(default_factory=Deadlines)
    breaker: Breaker = field(default_factory=Breaker)
    storage: Storage = field(default_factory=Storage)
    webhook: Webhook = field(default_factory=Webhook)
    metrics: Metrics = field(default_factory=Metrics)
    prometheus: Prometheus = field(default_factory=Prometheus)
    disk: Disk = field(default_factory=Disk)
    refresh: Refresh = field(default_factory=Refresh)
    resend: Resend = field(default_factory=Resend)
    secrets: Secrets = field(default_factory=Secrets)


SECTIONS = {f.name: f.type for f in fields(Config)}
RANGES = {   # (low, high) for each number, inclusive
    "schedule": {"grace_minutes": (1, 720), "catch_up_hours": (1, 168)},
    "budgets": {"daily_cap": (1, RESEND_DAILY_LIMIT), "daily_summary_reserve": (0, RESEND_DAILY_LIMIT), "daily_last_slots": (0, RESEND_DAILY_LIMIT), "monthly_cap": (1, RESEND_MONTHLY_LIMIT),
                "monthly_summary_reserve": (0, RESEND_MONTHLY_LIMIT), "monthly_last_slots": (0, RESEND_MONTHLY_LIMIT), "hourly_burst": (1, 100), "account_headroom_daily": (1, RESEND_DAILY_LIMIT),
                "account_headroom_monthly": (1, RESEND_MONTHLY_LIMIT)},
    "retry": {"max_attempts": (1, 10), "timeout_seconds": (1, 60), "jitter": (0, 0.5)},
    "deadlines": {"summary_hours": (0.25, 48), "alert_minutes": (1, 1440), "reminder_hours": (1, 168)},
    "breaker": {"failures_to_open": (1, 20), "cooldown_initial_minutes": (1, 1440), "cooldown_max_hours": (0.1, 72), "queue_max_rows": (10, 5000)},
    "storage": {"volume_mb": (8, 4096), "max_db_mb": (4, 4096), "wal_target_mb": (1, 512), "wal_refuse_mb": (1, 1024), "unfinished_payload_mb": (1, 512), "summary_reserve_mb": (0, 256),
                "finished_payload_days": (1, 3650), "finished_payload_mb": (1, 1024), "metadata_days": (1, 3650), "protected_days": (1, 3650), "protected_max_rows": (10, 100_000),
                "free_disk_floor_mb": (1, 100_000), "prune_at_percent": (50, 95)},
    "webhook": {"bind_port": (1, 65535), "max_body_kb": (1, 4096), "max_alerts": (1, 1000), "requests_per_minute": (1, 6000)},
    "metrics": {"bind_port": (1, 65535)},
    "prometheus": {"scrape_interval_seconds": (1, 300), "max_boundary_lag_intervals": (1, 100), "max_samples": (1000, 5_000_000), "timeout_seconds": (1, 300), "lookback_hours": (0.25, 48)},
    "disk": {"critical_free_percent": (1, 90), "recover_free_percent": (2, 95), "check_seconds": (10, 86400)},
    "refresh": {"max_files": (1, 500), "unfinished_minutes": (5, 1440)},
}


def _number(section: str, name: str, value, default):
    low, high = RANGES[section][name]
    kind = type(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind is int and not isinstance(value, int)):
        raise ConfigError(f"[{section}] {name} must be {'a whole number' if kind is int else 'a number'}, not {_shown(value)}")
    if not low <= value <= high:
        raise ConfigError(f"[{section}] {name} must be from {low} to {high}, not {value}")
    return value


def _shown(value) -> str:
    return repr(value)[:40] if isinstance(value, (str, int, float, bool)) else f"a value of type {type(value).__name__}"


def _text(section: str, name: str, value, limit: int = 300) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"[{section}] {name} must be text, not {_shown(value)}")
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(f"[{section}] {name} must be at most {limit} characters with no control characters")
    return value


def _build(section: str, cls, data) -> object:
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"[{section}] has an unknown setting: {unknown[0][:40]}")
    defaults = cls()
    values = {}
    for name, value in data.items():
        default = getattr(defaults, name)
        if name in RANGES.get(section, {}):
            values[name] = _number(section, name, value, default)
        elif name == "backoff_seconds":
            if not isinstance(value, list) or not 1 <= len(value) <= 10 or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 86_400 for v in value):
                raise ConfigError("[retry] backoff_seconds must be a list of 1 to 10 whole numbers of seconds, each from 1 to 86400")
            values[name] = tuple(value)
        else:
            values[name] = _text(section, name, value)
    return cls(**{**{f.name: getattr(defaults, f.name) for f in fields(cls)}, **values})


def _address(section: str, name: str, value: str, sender: bool = False) -> None:
    if sender:
        match = SENDER.fullmatch(value)
        address = (match.group("named") or match.group("bare")) if match else None
    else:
        address = value
    if not address or not ADDRESS.fullmatch(address) or len(address) > 254:
        raise ConfigError(f"[{section}] {name} must be an email address{' (optionally as Name <address>)' if sender else ''}")


def _bind(section: str, host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(f"[{section}] bind_host must be an IP address, not {_shown(host)}: a name can resolve to something wider than intended") from None
    if address.is_unspecified:
        raise ConfigError(f"[{section}] bind_host cannot be a wildcard address: the listener has no login of its own beyond a token, so it is given the one address it is reached on")


def check(config: Config) -> Config:
    """The checks that look at more than one setting, and the ones that need the outside (a zone, an address)."""
    e, s, b, st, w, m, p, d, r = config.email, config.schedule, config.budgets, config.storage, config.webhook, config.metrics, config.prometheus, config.disk, config.resend
    if not e.recipient or not e.sender:
        raise ConfigError("[email] recipient and sender are both required")
    _address("email", "recipient", e.recipient)
    _address("email", "sender", e.sender, sender=True)
    if not TIME.fullmatch(s.send_time):
        raise ConfigError(f"[schedule] send_time must be a time of day as HH:MM, not {_shown(s.send_time)}")
    try:
        ZoneInfo(s.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"[schedule] timezone must be a name from the IANA zone database, not {_shown(s.timezone)}") from None
    if b.daily_summary_reserve >= b.daily_cap or b.monthly_summary_reserve >= b.monthly_cap:
        raise ConfigError("[budgets] the summary reserve must be less than the cap, day and month")
    if b.daily_last_slots > b.daily_cap - b.daily_summary_reserve or b.monthly_last_slots > b.monthly_cap - b.monthly_summary_reserve:
        raise ConfigError("[budgets] the last slots must fit inside the shared pool (the cap less the summary reserve), day and month")
    if b.account_headroom_daily < b.daily_cap or b.account_headroom_monthly < b.monthly_cap:
        raise ConfigError("[budgets] the account headroom must be at least the notifier's own cap, day and month")
    if st.wal_refuse_mb < st.wal_target_mb:
        raise ConfigError("[storage] wal_refuse_mb must be at least wal_target_mb")
    if st.max_db_mb + st.wal_refuse_mb > st.volume_mb:
        raise ConfigError("[storage] max_db_mb and wal_refuse_mb together must fit in volume_mb, or the volume could fill before the ledger refuses")
    if st.free_disk_floor_mb + st.max_db_mb + st.wal_refuse_mb > st.volume_mb:
        raise ConfigError("[storage] free_disk_floor_mb is measured on the ledger's own volume, so it plus max_db_mb and wal_refuse_mb must fit in volume_mb, or every write would be refused")
    if st.summary_reserve_mb >= st.unfinished_payload_mb:
        raise ConfigError("[storage] summary_reserve_mb must be less than unfinished_payload_mb")
    if not st.ledger_path or not Path(st.ledger_path).is_absolute():
        raise ConfigError("[storage] ledger_path must be an absolute path")
    if config.refresh.results_dir and not Path(config.refresh.results_dir).is_absolute():
        raise ConfigError("[refresh] results_dir must be an absolute path, or empty for no result emails")
    if d.recover_free_percent <= d.critical_free_percent:
        raise ConfigError("[disk] recover_free_percent must be more than critical_free_percent")
    _bind("webhook", w.bind_host)
    _bind("metrics", m.bind_host)
    if (w.bind_host, w.bind_port) == (m.bind_host, m.bind_port):
        raise ConfigError("[webhook] and [metrics] cannot listen on the same address and port")
    if not (p.url.startswith("http://") or p.url.startswith("https://")):
        raise ConfigError("[prometheus] url must start with http:// or https://")
    for name in ("api_job", "node_job", "bandwidth_device"):            # these go into queries, so they are plain names and nothing else
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}", getattr(p, name)):
            raise ConfigError(f"[prometheus] {name} must be a plain name of letters, digits and _ . : -, up to 40 characters")
    if not r.api_base.startswith("https://") and not re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", r.api_base):
        raise ConfigError("[resend] api_base must be https (plain http is accepted only for a loopback address, which is how the tests run)")
    if not config.secrets.resend_api_key_file or not config.secrets.webhook_token_file:
        raise ConfigError("[secrets] resend_api_key_file and webhook_token_file are both required")
    return config


def from_mapping(data: dict) -> Config:
    """A Config from the parsed TOML. Raises ConfigError for anything that cannot work."""
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a table")
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise ConfigError(f"the configuration has an unknown section: {str(unknown[0])[:40]}")
    built = {name: _build(name, {"email": Email, "schedule": Schedule, "budgets": Budgets, "retry": Retry, "deadlines": Deadlines, "breaker": Breaker, "storage": Storage, "webhook": Webhook,
                                 "metrics": Metrics, "prometheus": Prometheus, "disk": Disk, "refresh": Refresh, "resend": Resend, "secrets": Secrets}[name], data[name]) for name in data}
    return check(Config(**built))


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigError(f"the configuration file cannot be read: {str(path)[:120]}") from None
    if len(raw) > 64 * 1024:
        raise ConfigError("the configuration file is too large")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"the configuration file is not valid TOML ({type(error).__name__})") from None
    return from_mapping(data)
