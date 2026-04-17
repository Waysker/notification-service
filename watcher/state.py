from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from .models import Alert, PriceObservation, PriceTrendAlert, PriceTrendType, TicketEvent
from .utils import now_iso


class StateStore:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def close(self) -> None:
        self._conn.close()

    def _setup(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                source TEXT NOT NULL,
                event_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                PRIMARY KEY(source, event_key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL,
                source TEXT NOT NULL,
                query TEXT NOT NULL,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                url TEXT NOT NULL,
                capacity_tb REAL,
                relevance REAL NOT NULL,
                observed_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_price_observations_item_time
            ON price_observations(item_key, observed_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_alert_state (
                item_key TEXT NOT NULL,
                trend_type TEXT NOT NULL,
                last_alert_at TEXT NOT NULL,
                PRIMARY KEY(item_key, trend_type)
            )
            """
        )
        self._conn.commit()

    def diff_and_upsert(self, events: list[TicketEvent]) -> list[Alert]:
        alerts: list[Alert] = []
        by_source: dict[str, dict[str, sqlite3.Row]] = {}
        for event in events:
            by_source.setdefault(event.source, {})
        for source in by_source:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE source = ?",
                (source,),
            ).fetchall()
            by_source[source] = {row["event_key"]: row for row in rows}

        ts = now_iso()
        for event in events:
            event = event.normalized()
            event_key = event.event_key
            payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
            fingerprint = payload

            prev_row = by_source[event.source].get(event_key)
            if prev_row is None:
                alerts.append(Alert(alert_type="new", event=event))
                self._conn.execute(
                    """
                    INSERT INTO events(source, event_key, payload_json, fingerprint, first_seen, last_seen)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (event.source, event_key, payload, fingerprint, ts, ts),
                )
                continue

            if prev_row["fingerprint"] != fingerprint:
                previous = TicketEvent(**json.loads(prev_row["payload_json"]))
                alerts.append(Alert(alert_type="changed", event=event, previous=previous))
                self._conn.execute(
                    """
                    UPDATE events
                    SET payload_json = ?, fingerprint = ?, last_seen = ?
                    WHERE source = ? AND event_key = ?
                    """,
                    (payload, fingerprint, ts, event.source, event_key),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE events
                    SET last_seen = ?
                    WHERE source = ? AND event_key = ?
                    """,
                    (ts, event.source, event_key),
                )

        self._conn.commit()
        return alerts

    @staticmethod
    def _parse_iso(raw: str) -> datetime | None:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _can_emit_price_alert(
        self,
        *,
        item_key: str,
        trend_type: str,
        now_ts: datetime,
        cooldown_hours: int,
    ) -> bool:
        row = self._conn.execute(
            """
            SELECT last_alert_at
            FROM price_alert_state
            WHERE item_key = ? AND trend_type = ?
            """,
            (item_key, trend_type),
        ).fetchone()
        if row is None:
            return True
        parsed = self._parse_iso(row["last_alert_at"])
        if parsed is None:
            return True
        return now_ts - parsed >= timedelta(hours=max(0, cooldown_hours))

    def _touch_price_alert_state(self, *, item_key: str, trend_type: str, ts: str) -> None:
        self._conn.execute(
            """
            INSERT INTO price_alert_state(item_key, trend_type, last_alert_at)
            VALUES(?, ?, ?)
            ON CONFLICT(item_key, trend_type)
            DO UPDATE SET last_alert_at = excluded.last_alert_at
            """,
            (item_key, trend_type, ts),
        )

    def record_price_observations(
        self,
        observations: list[PriceObservation],
        *,
        min_observations_for_trend: int,
        trend_window_size: int,
        drop_alert_percent: float,
        rise_alert_percent: float,
        alert_cooldown_hours: int,
    ) -> list[PriceTrendAlert]:
        alerts: list[PriceTrendAlert] = []
        if not observations:
            return alerts

        ts = now_iso()
        ts_dt = datetime.fromisoformat(ts)
        min_samples = max(2, min_observations_for_trend)
        window_size = max(min_samples, trend_window_size)

        # Keep only the best candidate for each item_key in a single run.
        by_item: dict[str, PriceObservation] = {}
        for observation in observations:
            normalized = observation.normalized()
            previous = by_item.get(normalized.item_key)
            if previous is None:
                by_item[normalized.item_key] = normalized
                continue
            if normalized.relevance > previous.relevance:
                by_item[normalized.item_key] = normalized
            elif normalized.relevance == previous.relevance and normalized.price < previous.price:
                by_item[normalized.item_key] = normalized

        for observation in by_item.values():
            rows = self._conn.execute(
                """
                SELECT price
                FROM price_observations
                WHERE item_key = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (observation.item_key, window_size),
            ).fetchall()
            history = [float(row["price"]) for row in rows]

            if len(history) >= min_samples:
                baseline = float(median(history))
                if baseline > 0:
                    change_percent = ((observation.price - baseline) / baseline) * 100.0
                    trend_type: PriceTrendType | None = None
                    if change_percent <= -abs(drop_alert_percent):
                        trend_type = "drop"
                    elif change_percent >= abs(rise_alert_percent):
                        trend_type = "rise"

                    if trend_type is not None and self._can_emit_price_alert(
                        item_key=observation.item_key,
                        trend_type=trend_type,
                        now_ts=ts_dt,
                        cooldown_hours=alert_cooldown_hours,
                    ):
                        alerts.append(
                            PriceTrendAlert(
                                trend_type=trend_type,
                                current=observation,
                                baseline_price=round(baseline, 2),
                                change_percent=round(change_percent, 2),
                                samples=len(history),
                            )
                        )
                        self._touch_price_alert_state(item_key=observation.item_key, trend_type=trend_type, ts=ts)

            self._conn.execute(
                """
                INSERT INTO price_observations(
                    item_key, source, query, title, price, currency, url, capacity_tb, relevance, observed_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.item_key,
                    observation.source,
                    observation.query,
                    observation.title,
                    observation.price,
                    observation.currency,
                    observation.url,
                    observation.capacity_tb,
                    observation.relevance,
                    ts,
                ),
            )

        self._conn.commit()
        return alerts
