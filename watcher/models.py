from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .utils import normalize_key, normalize_space, short_hash

Availability = Literal["available", "sold_out", "unknown"]
AlertType = Literal["new", "changed"]


@dataclass(frozen=True)
class TicketEvent:
    source: str
    play: str
    date: str
    time: str
    availability: Availability
    status_text: str
    url: str = ""
    venue: str = ""

    @property
    def event_key(self) -> str:
        core = f"{normalize_key(self.play)}|{self.date}|{self.time}"
        if self.date and self.time:
            return core
        return f"{core}|{short_hash(self.url)}"

    def normalized(self) -> "TicketEvent":
        return TicketEvent(
            source=normalize_space(self.source),
            play=normalize_space(self.play),
            date=normalize_space(self.date),
            time=normalize_space(self.time),
            availability=self.availability,
            status_text=normalize_space(self.status_text),
            url=normalize_space(self.url),
            venue=normalize_space(self.venue),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class Alert:
    alert_type: AlertType
    event: TicketEvent
    previous: TicketEvent | None = None

