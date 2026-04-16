from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .models import TicketEvent
from .utils import normalize_key, normalize_space


class SourceClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})

    def _get(self, url: str) -> str:
        response = self.session.get(url, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        return response.text

    def _get_json(self, url: str, params: dict[str, str | int] | None = None) -> dict:
        response = self.session.get(url, params=params, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"Graph API error: {payload['error']}")
        return payload

    @staticmethod
    def _play_matches(play_name: str, monitored_plays: Iterable[str]) -> bool:
        candidate = normalize_key(play_name)
        if not candidate:
            return False
        for monitored in monitored_plays:
            key = normalize_key(monitored)
            if not key:
                continue
            if key == candidate or key in candidate or candidate in key:
                return True
        return False

    def fetch_teatr_repertuar_events(self) -> list[TicketEvent]:
        html = self._get(self.settings.theater_repertuar_url)
        soup = BeautifulSoup(html, "html.parser")
        events: list[TicketEvent] = []

        for block in soup.select("div.spektakle-list div.block"):
            title_el = block.select_one("h2 a")
            if not title_el:
                continue

            play = normalize_space(title_el.get_text())
            if not self._play_matches(play, self.settings.monitored_plays):
                continue

            block_id = block.get("id", "")
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", block_id)
            date = date_match.group(1) if date_match else ""

            time_el = block.select_one("p.time")
            time = normalize_space(time_el.get_text()) if time_el else ""

            venue = ""
            desc_nodes = block.select("div.desc p")
            if desc_nodes:
                venue = normalize_space(desc_nodes[-1].get_text(" ", strip=True))

            ticket_button = block.select_one("div.tickets .btn")
            availability = "unknown"
            status_text = "Status nieznany"
            url = ""

            if ticket_button:
                status_text = normalize_space(ticket_button.get_text())
                classes = ticket_button.get("class", [])
                href = ticket_button.get("href", "") if ticket_button.name == "a" else ""
                if href:
                    url = urljoin(self.settings.theater_repertuar_url, href)

                status_lower = status_text.casefold()
                if "btn-disabled" in classes or "wyprzedane" in status_lower:
                    availability = "sold_out"
                elif "bilet" in status_lower and href:
                    availability = "available"

            events.append(
                TicketEvent(
                    source="teatr_repertuar",
                    play=play,
                    date=date,
                    time=time,
                    availability=availability,  # type: ignore[arg-type]
                    status_text=status_text,
                    url=url,
                    venue=venue,
                )
            )

        return events

    def fetch_teatr_ticket_listing_events(self) -> list[TicketEvent]:
        events: list[TicketEvent] = []
        link_re = re.compile(r"/index\.php/kup-bilet/")
        datetime_re = re.compile(r"-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})(?:$|[^0-9])")

        for play, slug in self.settings.play_slug_map.items():
            url = self.settings.theater_ticket_page_template.format(slug=slug)
            html = self._get(url)
            soup = BeautifulSoup(html, "html.parser")

            for link in soup.find_all("a", href=link_re):
                href = normalize_space(link.get("href", ""))
                if not href:
                    continue

                date = ""
                time = ""
                matches = datetime_re.findall(href)
                if matches:
                    y, m, d, hh, mm = matches[-1]
                    date = f"{y}-{m}-{d}"
                    time = f"{hh}:{mm}"

                events.append(
                    TicketEvent(
                        source="teatr_ticket_listing",
                        play=play,
                        date=date,
                        time=time,
                        availability="unknown",
                        status_text="Termin widoczny w systemie biletowym",
                        url=urljoin(url, href),
                    )
                )

        return events

    def fetch_biletomat_events(self) -> list[TicketEvent]:
        events: list[TicketEvent] = []
        for play, url in self.settings.biletomat_urls.items():
            html = self._get(url)
            start_dates = sorted(set(re.findall(r'"startDate":"([^"]+)"', html)))

            html_lower = html.casefold()
            all_closed = "sprzedaż zakończona" in html_lower and "kup bilet" not in html_lower

            for raw in start_dates:
                date = ""
                time = ""
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    date = dt.date().isoformat()
                    time = dt.strftime("%H:%M")
                except ValueError:
                    pass

                events.append(
                    TicketEvent(
                        source="biletomat",
                        play=play,
                        date=date,
                        time=time,
                        availability="sold_out" if all_closed else "unknown",
                        status_text=(
                            "sprzedaż zakończona"
                            if all_closed
                            else "termin widoczny na Biletomat (status niejednoznaczny)"
                        ),
                        url=url,
                    )
                )

        return events

    @staticmethod
    def _contains_any(text: str, terms: list[str]) -> bool:
        candidate = (text or "").casefold()
        for term in terms:
            if term and term.casefold() in candidate:
                return True
        return False

    @staticmethod
    def _first_match(text: str, terms: list[str]) -> str:
        candidate = (text or "").casefold()
        for term in terms:
            if term and term.casefold() in candidate:
                return term
        return ""

    def _fetch_facebook_feed(self, object_id: str, feed_path: str, source_name: str) -> list[TicketEvent]:
        url = (
            f"{self.settings.facebook_graph_base_url}/"
            f"{self.settings.facebook_graph_version}/{object_id}/{feed_path}"
        )
        params: dict[str, str | int] = {
            "fields": "id,message,created_time,permalink_url",
            "limit": self.settings.facebook_max_posts,
            "access_token": self.settings.facebook_access_token,
        }
        payload = self._get_json(url, params=params)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        events: list[TicketEvent] = []

        include_terms = self.settings.facebook_keywords_include or self.settings.monitored_plays
        exclude_terms = self.settings.facebook_keywords_exclude

        for item in items:
            message = normalize_space(item.get("message", ""))
            if not message:
                continue
            if include_terms and not self._contains_any(message, include_terms):
                continue
            if exclude_terms and self._contains_any(message, exclude_terms):
                continue

            created = item.get("created_time", "")
            date = ""
            time = ""
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    date = dt.date().isoformat()
                    time = dt.strftime("%H:%M")
                except ValueError:
                    pass

            play = self._first_match(message, self.settings.monitored_plays)
            if not play:
                play = self._first_match(message, include_terms) or "Facebook"

            url = normalize_space(item.get("permalink_url", ""))
            short_message = message if len(message) <= 280 else f"{message[:277]}..."
            status_text = f"FB post: {short_message}"

            events.append(
                TicketEvent(
                    source=source_name,
                    play=play,
                    date=date,
                    time=time,
                    availability="unknown",
                    status_text=status_text,
                    url=url,
                )
            )

        return events

    def fetch_facebook_events(self) -> list[TicketEvent]:
        if not self.settings.enable_facebook:
            return []
        if not self.settings.facebook_access_token:
            raise RuntimeError("FACEBOOK_ACCESS_TOKEN missing")
        if not (self.settings.facebook_page_id or self.settings.facebook_group_id):
            raise RuntimeError("FACEBOOK_PAGE_ID / FACEBOOK_GROUP_ID missing")

        events: list[TicketEvent] = []
        if self.settings.facebook_page_id:
            events.extend(
                self._fetch_facebook_feed(
                    object_id=self.settings.facebook_page_id,
                    feed_path="posts",
                    source_name="facebook_page",
                )
            )
        if self.settings.facebook_group_id:
            events.extend(
                self._fetch_facebook_feed(
                    object_id=self.settings.facebook_group_id,
                    feed_path="feed",
                    source_name="facebook_group",
                )
            )
        return events
