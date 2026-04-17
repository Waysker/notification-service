from __future__ import annotations

import argparse
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from .config import load_settings
from .models import PriceObservation, TicketEvent
from .notifier import (
    EmailNotifier,
    NtfyNotifier,
    SignalNotifier,
    TelegramNotifier,
    format_alerts,
    format_alerts_plain,
    format_price_alerts,
    format_price_alerts_plain,
)
from .sources import SourceClient
from .state import StateStore
from .utils import now_iso

logger = logging.getLogger("bilety-watcher")


@dataclass(frozen=True)
class NotifierBundle:
    ntfy: NtfyNotifier
    signal: SignalNotifier
    email: EmailNotifier
    telegram: TelegramNotifier


def _dedupe(events: list[TicketEvent]) -> list[TicketEvent]:
    by_key: dict[tuple[str, str], TicketEvent] = {}
    for event in events:
        by_key[(event.source, event.event_key)] = event
    return list(by_key.values())


def _split_message(message: str, chunk_size: int = 3800) -> list[str]:
    if len(message) <= chunk_size:
        return [message]
    lines = message.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > chunk_size and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line) + 1
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _build_notifiers(settings) -> NotifierBundle:
    return NotifierBundle(
        ntfy=NtfyNotifier(
            server=settings.ntfy_server,
            topic=settings.ntfy_topic,
            token=settings.ntfy_token,
            username=settings.ntfy_username,
            password=settings.ntfy_password,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        signal=SignalNotifier(
            cli_path=settings.signal_cli_path,
            account=settings.signal_account,
            recipients=settings.signal_recipients,
            timeout_seconds=settings.signal_timeout_seconds,
        ),
        email=EmailNotifier(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_username=settings.smtp_username,
            smtp_password=settings.smtp_password,
            smtp_use_tls=settings.smtp_use_tls,
            sender=settings.email_from,
            recipients=settings.email_to,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        telegram=TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.request_timeout_seconds,
        ),
    )


def _fetch_events(client: SourceClient) -> list[TicketEvent]:
    events: list[TicketEvent] = []
    jobs: list[tuple[str, Callable[[], list[TicketEvent]]]] = [
        ("teatr_repertuar", client.fetch_teatr_repertuar_events),
        ("teatr_ticket_listing", client.fetch_teatr_ticket_listing_events),
    ]
    if client.settings.enable_biletomat and client.settings.biletomat_urls:
        jobs.append(("biletomat", client.fetch_biletomat_events))
    if client.settings.enable_facebook:
        jobs.append(("facebook", client.fetch_facebook_events))

    for source_name, fn in jobs:
        try:
            source_events = fn()
            logger.info("%s: fetched %d events", source_name, len(source_events))
            events.extend(source_events)
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s: fetch failed: %s", source_name, exc)
    return _dedupe(events)


def _fetch_price_observations(client: SourceClient) -> list[PriceObservation]:
    if not client.settings.enable_price_monitoring:
        return []
    if not client.settings.price_source_urls:
        return []
    return client.fetch_price_observations()


def _build_smoke_report(
    settings,
    passed: list[str],
    failed: list[str],
    warnings: list[str],
) -> str:
    ts = now_iso()
    lines: list[str] = [f"SMOKE CHECK | {ts}", ""]
    if passed:
        lines.append("OK:")
        for item in passed:
            lines.append(f"- {item}")
        lines.append("")
    if warnings:
        lines.append("WARN:")
        for item in warnings:
            lines.append(f"- {item}")
        lines.append("")
    if failed:
        lines.append("FAIL:")
        for item in failed:
            lines.append(f"- {item}")
        lines.append("")
    lines.append(
        (
            "notifications: "
            f"ntfy={settings.ntfy_enabled}, "
            f"signal={settings.signal_enabled}, "
            f"email={settings.email_enabled}, "
            f"telegram={settings.telegram_enabled}"
        )
    )
    lines.append(
        "sources: "
        f"biletomat={settings.enable_biletomat}, "
        f"facebook={settings.enable_facebook}, "
        f"price_monitoring={settings.enable_price_monitoring and bool(settings.price_source_urls)}"
    )
    return "\n".join(lines).strip()


def _build_email_subject(settings, suffix: str) -> str:
    prefix = settings.email_subject_prefix or "[bilety]"
    return f"{prefix} {suffix}".strip()


def _send_notification_with_fallback(
    ntfy_notifier: NtfyNotifier,
    signal_notifier: SignalNotifier,
    email_notifier: EmailNotifier,
    telegram_notifier: TelegramNotifier,
    *,
    plain_text: str,
    html_text: str,
    email_subject: str,
    ntfy_title: str,
    ntfy_priority: str,
    ntfy_tags: list[str],
    allow_email_fallback: bool = True,
) -> str | None:
    if ntfy_notifier.enabled:
        try:
            ntfy_notifier.send_text(
                plain_text,
                title=ntfy_title,
                priority=ntfy_priority,
                tags=ntfy_tags,
            )
            logger.info("ntfy notification sent")
            return "ntfy"
        except Exception as exc:  # noqa: BLE001
            logger.exception("ntfy notification failed: %s", exc)

    if signal_notifier.enabled:
        try:
            signal_notifier.send_text(plain_text)
            logger.info("signal notification sent")
            return "signal"
        except Exception as exc:  # noqa: BLE001
            logger.exception("signal notification failed: %s", exc)

    if allow_email_fallback and email_notifier.enabled:
        try:
            email_notifier.send_text(subject=email_subject, text=plain_text)
            logger.info("email notification sent")
            return "email"
        except Exception as exc:  # noqa: BLE001
            logger.exception("email notification failed: %s", exc)

    if telegram_notifier.enabled:
        try:
            for chunk in _split_message(html_text):
                telegram_notifier.send_text(chunk)
            logger.info("telegram notification sent")
            return "telegram"
        except Exception as exc:  # noqa: BLE001
            logger.exception("telegram notification failed: %s", exc)

    return None


def run_smoke_check(force_notify: bool = False) -> int:
    settings = load_settings()
    client = SourceClient(settings)
    notifiers = _build_notifiers(settings)

    passed: list[str] = []
    failed: list[str] = []
    warnings: list[str] = []

    checks: list[tuple[str, Callable[[], list[TicketEvent]]]] = [
        ("teatr_repertuar", client.fetch_teatr_repertuar_events),
        ("teatr_ticket_listing", client.fetch_teatr_ticket_listing_events),
    ]
    if settings.enable_biletomat and settings.biletomat_urls:
        checks.append(("biletomat", client.fetch_biletomat_events))
    if settings.enable_facebook:
        checks.append(("facebook", client.fetch_facebook_events))
    if settings.enable_price_monitoring and settings.price_source_urls:
        checks.append(("price_monitoring", lambda: []))

    for check_name, fn in checks:
        if check_name == "price_monitoring":
            try:
                observations = _fetch_price_observations(client)
                passed.append(f"price_monitoring: fetch ok ({len(observations)} observations)")
                if not observations:
                    warnings.append("price_monitoring: 0 obserwacji (sprawdź PRICE_SOURCE_URLS / selekcję)")
            except Exception as exc:  # noqa: BLE001
                logger.exception("smoke %s failed: %s", check_name, exc)
                failed.append(f"{check_name}: {exc}")
            continue
        try:
            events = fn()
            passed.append(f"{check_name}: fetch ok ({len(events)} events)")
            if check_name == "teatr_repertuar" and not events:
                warnings.append("teatr_repertuar: 0 events po filtrze (możliwe poza repertuarem)")
            if check_name == "teatr_ticket_listing" and not events:
                warnings.append("teatr_ticket_listing: 0 events po filtrze")
            if check_name == "facebook" and not events:
                warnings.append("facebook: 0 pasujących postów (sprawdź filtry słów)")
        except Exception as exc:  # noqa: BLE001
            logger.exception("smoke %s failed: %s", check_name, exc)
            failed.append(f"{check_name}: {exc}")

    report = _build_smoke_report(settings, passed, failed, warnings)
    print(report)

    should_notify_success = (force_notify or settings.smoke_notify_on_success) and not failed
    should_notify_failure = (force_notify or settings.smoke_notify_on_failure) and bool(failed)
    if should_notify_success or should_notify_failure:
        smoke_failed = bool(failed)
        html_report = (
            "<b>SMOKE CHECK</b>\n<pre>"
            + report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</pre>"
        )
        route = _send_notification_with_fallback(
            ntfy_notifier=notifiers.ntfy,
            signal_notifier=notifiers.signal,
            email_notifier=notifiers.email,
            telegram_notifier=notifiers.telegram,
            plain_text=f"SMOKE CHECK\n\n{report}",
            html_text=html_report,
            email_subject=_build_email_subject(settings, "Smoke check"),
            ntfy_title=f"Notification tracker: smoke {'FAIL' if smoke_failed else 'OK'}",
            ntfy_priority=(
                settings.ntfy_priority_smoke_failure if smoke_failed else settings.ntfy_priority_smoke_success
            ),
            ntfy_tags=settings.ntfy_tags_smoke,
            allow_email_fallback=True,
        )
        if route is None:
            logger.warning("smoke report generated but no notification channel is configured/working")

    return 1 if failed else 0


def run_once(print_events: bool = False, dry_run: bool = False) -> int:
    settings = load_settings()
    client = SourceClient(settings)
    store = StateStore(settings.state_db_path)
    notifiers = _build_notifiers(settings)

    try:
        ticket_events = _fetch_events(client)
        logger.info("total ticket events: %d", len(ticket_events))
        price_observations = _fetch_price_observations(client)
        logger.info("total price observations: %d", len(price_observations))

        if print_events:
            grouped = defaultdict(list)
            for event in sorted(ticket_events, key=lambda e: (e.play.casefold(), e.date, e.time, e.source)):
                grouped[event.play].append(event.to_dict())
            payload = {
                "tickets": grouped,
                "prices": [obs.to_dict() for obs in price_observations],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))

        ticket_alerts = store.diff_and_upsert(ticket_events)
        price_alerts = store.record_price_observations(
            price_observations,
            min_observations_for_trend=settings.price_min_observations_for_trend,
            trend_window_size=settings.price_trend_window_size,
            drop_alert_percent=settings.price_drop_alert_percent,
            rise_alert_percent=settings.price_rise_alert_percent,
            alert_cooldown_hours=settings.price_alert_cooldown_hours,
        )

        if not ticket_alerts and not price_alerts:
            logger.info("no changes detected")
            return 0

        logger.info(
            "alerts generated: ticket=%d, price=%d",
            len(ticket_alerts),
            len(price_alerts),
        )

        html_parts: list[str] = []
        plain_parts: list[str] = []
        if ticket_alerts:
            html_parts.append(format_alerts(ticket_alerts))
            plain_parts.append(format_alerts_plain(ticket_alerts))
        if price_alerts:
            html_parts.append(format_price_alerts(price_alerts))
            plain_parts.append(format_price_alerts_plain(price_alerts))

        message_html = "\n\n".join(part for part in html_parts if part.strip())
        message_plain = "\n\n".join(part for part in plain_parts if part.strip())
        print(message_plain)

        if dry_run:
            logger.info("dry-run enabled, notification send skipped")
            return 0

        if ticket_alerts and not price_alerts:
            ntfy_priority = settings.ntfy_priority_alerts
            ntfy_title = "Notification tracker: ticket alerts"
            allow_email_fallback = settings.email_fallback_on_ticket_alerts
            email_subject = _build_email_subject(settings, "Nowe alerty biletowe")
            ntfy_tags = settings.ntfy_tags_alerts
        elif price_alerts and not ticket_alerts:
            ntfy_priority = settings.ntfy_priority_price_alerts
            ntfy_title = "Notification tracker: price trend alerts"
            allow_email_fallback = settings.email_fallback_on_price_alerts
            email_subject = _build_email_subject(settings, "Alert trendu cen")
            ntfy_tags = settings.ntfy_tags_price_alerts
        else:
            ntfy_priority = settings.ntfy_priority_alerts
            ntfy_title = "Notification tracker: combined alerts"
            allow_email_fallback = settings.email_fallback_on_ticket_alerts or settings.email_fallback_on_price_alerts
            email_subject = _build_email_subject(settings, "Alerty (tickets + prices)")
            ntfy_tags = []
            for tag in settings.ntfy_tags_alerts + settings.ntfy_tags_price_alerts:
                if tag and tag not in ntfy_tags:
                    ntfy_tags.append(tag)

        route = _send_notification_with_fallback(
            ntfy_notifier=notifiers.ntfy,
            signal_notifier=notifiers.signal,
            email_notifier=notifiers.email,
            telegram_notifier=notifiers.telegram,
            plain_text=message_plain,
            html_text=message_html,
            email_subject=email_subject,
            ntfy_title=ntfy_title,
            ntfy_priority=ntfy_priority,
            ntfy_tags=ntfy_tags,
            allow_email_fallback=allow_email_fallback,
        )
        if route is None:
            logger.warning("no notification channel configured/working; alert printed only")

        return 0
    finally:
        store.close()


def watch_loop(print_events: bool = False, dry_run: bool = False) -> int:
    settings = load_settings()
    logger.info("watch loop started; interval=%ss", settings.check_interval_seconds)
    while True:
        logger.info("run started at %s", now_iso())
        exit_code = run_once(print_events=print_events, dry_run=dry_run)
        if exit_code != 0:
            return exit_code
        time.sleep(settings.check_interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notification tracker: bilety + trend cen")
    sub = parser.add_subparsers(dest="command", required=True)

    run_once_cmd = sub.add_parser("run-once", help="Jedno sprawdzenie i ewentualne alerty")
    run_once_cmd.add_argument("--dry-run", action="store_true", help="Nie wysyłaj notyfikacji")
    run_once_cmd.add_argument("--print-events", action="store_true", help="Wypisz surowe eventy")

    watch_cmd = sub.add_parser("watch", help="Pętla watch co CHECK_INTERVAL_SECONDS")
    watch_cmd.add_argument("--dry-run", action="store_true", help="Nie wysyłaj notyfikacji")
    watch_cmd.add_argument("--print-events", action="store_true", help="Wypisz surowe eventy")

    sub.add_parser("test-ntfy", help="Wyślij testową wiadomość ntfy")
    sub.add_parser("test-telegram", help="Wyślij testową wiadomość Telegram")
    sub.add_parser("test-signal", help="Wyślij testową wiadomość Signal")
    sub.add_parser("test-email", help="Wyślij testowy email")
    smoke_cmd = sub.add_parser("smoke-check", help="Dzienne sanity/smoke check parserów i źródeł")
    smoke_cmd.add_argument("--notify", action="store_true", help="Wymuś wysyłkę raportu smoke")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = parse_args()

    if args.command == "run-once":
        return run_once(print_events=args.print_events, dry_run=args.dry_run)
    if args.command == "watch":
        return watch_loop(print_events=args.print_events, dry_run=args.dry_run)
    if args.command == "test-ntfy":
        settings = load_settings()
        notifier = NtfyNotifier(
            server=settings.ntfy_server,
            topic=settings.ntfy_topic,
            token=settings.ntfy_token,
            username=settings.ntfy_username,
            password=settings.ntfy_password,
            timeout_seconds=settings.request_timeout_seconds,
        )
        if not notifier.enabled:
            logger.error("NTFY_SERVER / NTFY_TOPIC are missing")
            return 2
        notifier.send_text(
            "Test: notification tracker działa i może wysyłać alerty przez ntfy.",
            title="Notification tracker: test ntfy",
            priority=settings.ntfy_priority_alerts,
            tags=settings.ntfy_tags_alerts,
        )
        print("Test ntfy wysłany.")
        return 0
    if args.command == "test-telegram":
        settings = load_settings()
        notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.request_timeout_seconds,
        )
        if not notifier.enabled:
            logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing")
            return 2
        notifier.send_text("Test: watcher działa i może wysyłać alerty.")
        print("Test wysłany.")
        return 0
    if args.command == "test-signal":
        settings = load_settings()
        notifier = SignalNotifier(
            cli_path=settings.signal_cli_path,
            account=settings.signal_account,
            recipients=settings.signal_recipients,
            timeout_seconds=settings.signal_timeout_seconds,
        )
        if not notifier.enabled:
            logger.error("SIGNAL_ACCOUNT / SIGNAL_RECIPIENTS are missing")
            return 2
        notifier.send_text("Test: watcher działa i może wysyłać alerty przez Signal.")
        print("Test Signal wysłany.")
        return 0
    if args.command == "test-email":
        settings = load_settings()
        notifier = EmailNotifier(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_username=settings.smtp_username,
            smtp_password=settings.smtp_password,
            smtp_use_tls=settings.smtp_use_tls,
            sender=settings.email_from,
            recipients=settings.email_to,
            timeout_seconds=settings.request_timeout_seconds,
        )
        if not notifier.enabled:
            logger.error("SMTP_HOST / EMAIL_FROM / EMAIL_TO are missing")
            return 2
        notifier.send_text(
            subject=_build_email_subject(settings, "Test"),
            text="Test: watcher działa i może wysyłać alerty email.",
        )
        print("Test email wysłany.")
        return 0
    if args.command == "smoke-check":
        return run_smoke_check(force_notify=args.notify)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
