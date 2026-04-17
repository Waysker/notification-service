from __future__ import annotations

import html
import subprocess
from email.message import EmailMessage
import smtplib
from datetime import datetime

import requests

from .models import Alert, PriceTrendAlert
from .utils import normalize_space


def _availability_label(value: str) -> str:
    mapping = {
        "available": "DOSTĘPNE",
        "sold_out": "WYPRZEDANE",
        "unknown": "NIEZNANE",
    }
    return mapping.get(value, value.upper())


def _build_alert_lines(alerts: list[Alert], html_mode: bool) -> list[str]:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if html_mode:
        lines: list[str] = [f"<b>Bilety update</b> ({ts})", ""]
    else:
        lines = [f"Bilety update ({ts})", ""]

    for alert in alerts:
        event = alert.event
        prefix = "NOWE" if alert.alert_type == "new" else "ZMIANA"
        if html_mode:
            lines.append(
                (
                    f"• <b>{prefix}</b> | {html.escape(event.play)} | {event.date} {event.time} | "
                    f"{_availability_label(event.availability)} | {html.escape(event.source)}"
                )
            )
        else:
            lines.append(
                (
                    f"* {prefix} | {event.play} | {event.date} {event.time} | "
                    f"{_availability_label(event.availability)} | {event.source}"
                )
            )
        if alert.previous is not None and alert.previous.availability != event.availability:
            lines.append(
                f"  status: {_availability_label(alert.previous.availability)} -> "
                f"{_availability_label(event.availability)}"
            )
        if event.status_text:
            text = normalize_space(event.status_text)
            if html_mode:
                text = html.escape(text)
            lines.append(f"  info: {text}")
        if event.url:
            if html_mode:
                lines.append(f"  <a href=\"{html.escape(event.url)}\">link</a>")
            else:
                lines.append(f"  link: {event.url}")
        lines.append("")

    return lines


def format_alerts(alerts: list[Alert]) -> str:
    lines = _build_alert_lines(alerts, html_mode=True)
    return "\n".join(lines).strip()


def format_alerts_plain(alerts: list[Alert]) -> str:
    lines = _build_alert_lines(alerts, html_mode=False)
    return "\n".join(lines).strip()


def _trend_label(value: str) -> str:
    if value == "drop":
        return "SPADEK"
    if value == "rise":
        return "WZROST"
    return value.upper()


def _capacity_label(capacity_tb: float | None) -> str:
    if capacity_tb is None:
        return "?"
    if abs(capacity_tb - round(capacity_tb)) < 0.01:
        return f"{int(round(capacity_tb))}TB"
    return f"{capacity_tb:.1f}TB"


def _build_price_alert_lines(alerts: list[PriceTrendAlert], html_mode: bool) -> list[str]:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if html_mode:
        lines: list[str] = [f"<b>Price trend update</b> ({ts})", ""]
    else:
        lines = [f"Price trend update ({ts})", ""]

    for alert in alerts:
        current = alert.current
        trend = _trend_label(alert.trend_type)
        sign = "+" if alert.change_percent > 0 else ""
        baseline = f"{alert.baseline_price:.2f} {current.currency}"
        current_price = f"{current.price:.2f} {current.currency}"
        cap = _capacity_label(current.capacity_tb)
        line = (
            f"{trend} | {current.title} | {current_price} | "
            f"vs mediana {baseline} ({sign}{alert.change_percent:.2f}%) | cap~{cap}"
        )
        if html_mode:
            lines.append(f"• {html.escape(line)}")
            if current.url:
                lines.append(f"  <a href=\"{html.escape(current.url)}\">link</a>")
        else:
            lines.append(f"* {line}")
            if current.url:
                lines.append(f"  link: {current.url}")
        lines.append(f"  source: {current.source}, samples={alert.samples}, relevance={current.relevance:.2f}")
        lines.append("")
    return lines


def format_price_alerts(alerts: list[PriceTrendAlert]) -> str:
    lines = _build_price_alert_lines(alerts, html_mode=True)
    return "\n".join(lines).strip()


def format_price_alerts_plain(alerts: list[PriceTrendAlert]) -> str:
    lines = _build_price_alert_lines(alerts, html_mode=False)
    return "\n".join(lines).strip()


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: int = 20) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = requests.post(url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()


class NtfyNotifier:
    def __init__(
        self,
        server: str,
        topic: str,
        token: str = "",
        username: str = "",
        password: str = "",
        timeout_seconds: int = 20,
    ) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.server and self.topic)

    def send_text(
        self,
        text: str,
        *,
        title: str = "",
        priority: str = "",
        tags: list[str] | None = None,
    ) -> None:
        if not self.enabled:
            return

        headers: dict[str, str] = {}
        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = ",".join(tags)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        auth = None
        if not self.token and self.username and self.password:
            auth = (self.username, self.password)

        url = f"{self.server}/{self.topic}"
        response = requests.post(
            url,
            data=text.encode("utf-8"),
            headers=headers,
            auth=auth,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()


class SignalNotifier:
    def __init__(
        self,
        cli_path: str,
        account: str,
        recipients: list[str],
        timeout_seconds: int = 30,
    ) -> None:
        self.cli_path = cli_path
        self.account = account
        self.recipients = recipients
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.account and self.recipients)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return

        cmd = [
            self.cli_path,
            "-a",
            self.account,
            "send",
            "-m",
            text,
            *self.recipients,
        ]
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"signal-cli not found: {self.cli_path}. Ustaw SIGNAL_CLI_PATH albo zainstaluj signal-cli."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(f"signal-cli send failed: {stderr or exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("signal-cli send timed out") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"signal-cli send failed: {stderr or result.returncode}")


class EmailNotifier:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_password: str,
        smtp_use_tls: bool,
        sender: str,
        recipients: list[str],
        timeout_seconds: int = 20,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.smtp_use_tls = smtp_use_tls
        self.sender = sender
        self.recipients = recipients
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.smtp_host and self.sender and self.recipients)

    def send_text(self, subject: str, text: str) -> None:
        if not self.enabled:
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(text)

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout_seconds) as smtp:
            if self.smtp_use_tls:
                smtp.starttls()
            if self.smtp_username and self.smtp_password:
                smtp.login(self.smtp_username, self.smtp_password)
            smtp.send_message(msg)
