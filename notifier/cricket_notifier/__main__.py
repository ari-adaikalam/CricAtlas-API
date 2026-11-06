"""The command line: run the service, check a configuration, read the ledger, review or resend a failed message, write the Prometheus rules.

    python -m cricket_notifier run --config /etc/cricket-notifier/config.toml
    python -m cricket_notifier check-config --config ...
    python -m cricket_notifier status --config ...
    python -m cricket_notifier review ID --config ...
    python -m cricket_notifier resend ID --config ...
    python -m cricket_notifier rules [--write PATH] --config ...

Everything except `run` and `resend` only reads the ledger or the configuration and needs no network and no secret. Nothing here prints a secret, an address or the text of a message.
"""
import argparse
import logging
import signal
import sys
import threading
import time

from cricket_notifier import config as config_module
from cricket_notifier import rules as rules_module
from cricket_notifier.bandwidth import BandwidthCollector
from cricket_notifier.config import ConfigError
from cricket_notifier.errors import IllegalTransition, StorageUnavailable
from cricket_notifier.ledger import Ledger
from cricket_notifier.metrics import Metrics, MetricsServer
from cricket_notifier.prometheus import PrometheusClient
from cricket_notifier.render import Renderer
from cricket_notifier.resend import ResendClient
from cricket_notifier.secrets_io import load_secrets
from cricket_notifier.sender import Sender
from cricket_notifier.service import Service
from cricket_notifier.summary import SummaryBuilder
from cricket_notifier.watcher import Watcher
from cricket_notifier.webhook import WebhookServer

log = logging.getLogger("cricket_notifier")


def assemble(config, secrets, clock=time.time):
    """Every part of the service, joined. Nothing starts listening here."""
    ledger = Ledger(config, clock)
    renderer = Renderer(config)
    source = PrometheusClient(config.prometheus.url, config.prometheus.timeout_seconds, config.prometheus.max_samples)
    builder = SummaryBuilder(source, ledger, config)
    sender = Sender(ledger, ResendClient(config.resend.api_base, secrets.resend_api_key, config.retry.timeout_seconds), clock)
    watcher = Watcher(ledger, renderer, config)
    bandwidth = BandwidthCollector(source, ledger, config, clock)
    webhook = WebhookServer(config, ledger, renderer, secrets.webhook_token, clock)
    metrics = Metrics(config, ledger, webhook, renderer, clock)
    service = Service(config, ledger, renderer, builder, sender, watcher, bandwidth, metrics, clock)
    webhook.on_stored = service.wake.set
    return service, webhook, MetricsServer(config, metrics), ledger


def run(config) -> int:
    secrets = load_secrets(config.secrets.resend_api_key_file, config.secrets.webhook_token_file)
    service, webhook, metrics_server, ledger = assemble(config, secrets)
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: (stop.set(), service.wake.set()))
    webhook.start()
    metrics_server.start()
    log.info("started: webhook on %s:%s, metrics on %s:%s", config.webhook.bind_host, webhook.port, config.metrics.bind_host, metrics_server.port)
    try:
        service.run_forever(stop)
    finally:
        webhook.stop()
        metrics_server.stop()
        ledger.close()
        log.info("stopped")
    return 0


def _ledger(config) -> Ledger:
    return Ledger(config, time.time)


def status(config) -> int:
    ledger = _ledger(config)
    try:
        now = time.time()
        counts = ledger.counts()
        print("messages by kind and state:")
        for (kind, state), n in sorted(counts.items()):
            print(f"  {kind:12} {state:18} {n}")
        if not counts:
            print("  none")
        breaker, storage, usage = ledger.breaker(), ledger.storage_status(), ledger.usage(now)
        print(f"breaker: {breaker.get('state', 'closed')} ({breaker.get('failures', 0)} failures in a row)")
        print(f"storage: {storage.mode}, {storage.used_bytes} bytes in use, wal {storage.wal_bytes}, free {storage.free_bytes}")
        print(f"charged today (UTC): {dict(usage['day'])}   this month: {dict(usage['month'])}")
        report = ledger.overdue_report(now, config.schedule.grace_minutes * 60)
        print(f"summary overdue: {int(report['summary_overdue_seconds'])} s, failed or unconfirmed: {report['failed_or_unconfirmed']}")
        needs = ledger.read("SELECT id, kind, state, key FROM messages WHERE state IN ('failed','unconfirmed') AND kind NOT IN ('email_path','host') ORDER BY id")
        for row in needs:
            print(f"  needs review: id {row['id']} {row['kind']} {row['state']} {row['key'][:60]}")
    finally:
        ledger.close()
    return 0


def review(config, msg_id: int) -> int:
    ledger = _ledger(config)
    try:
        ledger.review(msg_id, time.time())
    except IllegalTransition as error:
        print(f"cannot review message {msg_id}: {error}", file=sys.stderr)
        return 1
    finally:
        ledger.close()
    print(f"message {msg_id} is marked reviewed")
    return 0


def resend(config, msg_id: int) -> int:
    ledger = _ledger(config)
    try:
        new_id, same_key = ledger.manual_resend(msg_id, time.time())
    except IllegalTransition as error:
        print(f"cannot resend message {msg_id}: {error}", file=sys.stderr)
        return 1
    finally:
        ledger.close()
    if same_key:
        print(f"message {msg_id} will be sent again with its original key, so Resend cannot accept it twice. The running service sends it.")
    else:
        print(f"WARNING: the original key has expired (24 hours), so the resend is message {new_id} with a new key, and the recipient may get the email twice if the first one did arrive. The running service sends it.")
    return 0


def rules(config, write: str | None) -> int:
    text = rules_module.render_yaml(config.prometheus.api_job, f"{int(config.prometheus.scrape_interval_seconds)}s")
    if write:
        with open(write, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {write}")
    else:
        sys.stdout.write(text)
    return 0


def check_config(config) -> int:
    config_module.check(config)
    load_secrets(config.secrets.resend_api_key_file, config.secrets.webhook_token_file)
    print("configuration ok: the settings are valid and both secret files are readable and private")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cricket_notifier", description="Monitoring emails for the Cricket API.")
    parser.add_argument("command", choices=("run", "check-config", "status", "review", "resend", "rules"))
    parser.add_argument("id", nargs="?", type=int, help="the message id, for review and resend")
    parser.add_argument("--config", required=True)
    parser.add_argument("--write", help="rules: write to this file instead of printing")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command in ("review", "resend") and args.id is None:
        parser.error(f"{args.command} needs a message id")
    try:
        config = config_module.load(args.config)
        if args.command == "run":
            return run(config)
        if args.command == "check-config":
            return check_config(config)
        if args.command == "status":
            return status(config)
        if args.command == "review":
            return review(config, args.id)
        if args.command == "resend":
            return resend(config, args.id)
        return rules(config, args.write)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except StorageUnavailable as error:
        print(f"the ledger cannot be used: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"cannot start: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
