from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .utils import normalize_key, normalize_space, short_hash

Availability = Literal["available", "sold_out", "unknown"]
AlertType = Literal["new", "changed"]
PriceTrendType = Literal["drop", "rise"]


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


@dataclass(frozen=True)
class PriceObservation:
    source: str
    query: str
    title: str
    price: float
    currency: str
    url: str
    capacity_tb: float | None
    relevance: float

    @property
    def item_key(self) -> str:
        base = normalize_key(self.title)
        if not base:
            base = short_hash(self.url or f"{self.source}|{self.query}")
        if self.capacity_tb is not None:
            return f"{base}|{self.capacity_tb:.2f}tb"
        return base

    def normalized(self) -> "PriceObservation":
        currency = normalize_space(self.currency).upper()
        if not currency:
            currency = "PLN"
        return PriceObservation(
            source=normalize_space(self.source),
            query=normalize_space(self.query),
            title=normalize_space(self.title),
            price=round(float(self.price), 2),
            currency=currency,
            url=normalize_space(self.url),
            capacity_tb=self.capacity_tb,
            relevance=max(0.0, min(1.0, float(self.relevance))),
        )

    def to_dict(self) -> dict[str, str | float | None]:
        return asdict(self.normalized())


@dataclass(frozen=True)
class PriceTrendAlert:
    trend_type: PriceTrendType
    current: PriceObservation
    baseline_price: float
    change_percent: float
    samples: int
