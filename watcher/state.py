from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Alert, TicketEvent
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

