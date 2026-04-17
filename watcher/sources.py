from __future__ import annotations

import json
import logging
import re
import struct
import zlib
from collections import deque
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .models import PriceObservation, TicketEvent
from .utils import normalize_key, normalize_space, short_hash

PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00A0.]?\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(zł|pln|eur|€|usd|\$)",
    re.IGNORECASE,
)
TB_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*tb\b", re.IGNORECASE)
GB_RE = re.compile(r"(\d{3,5})\s*gb\b", re.IGNORECASE)
PCPARTPICKER_TREND_IMG_RE = re.compile(
    r"/static/forever/images/trends/[^\"'\s>]+\.png",
    re.IGNORECASE,
)

logger = logging.getLogger("bilety-watcher")


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

    @staticmethod
    def _parse_price_amount(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        raw = normalize_space(str(value))
        if not raw:
            return None

        amount_match = re.search(r"\d[\d\s\u00A0.,]*", raw)
        if not amount_match:
            return None

        amount_raw = amount_match.group(0).replace("\u00A0", "").replace(" ", "").replace(",", ".")
        if amount_raw.count(".") > 1:
            # "1.999.99" -> "1999.99"
            head, tail = amount_raw.rsplit(".", 1)
            amount_raw = head.replace(".", "") + "." + tail
        try:
            return round(float(amount_raw), 2)
        except ValueError:
            return None

    @staticmethod
    def _extract_capacity_tb(text: str) -> float | None:
        candidate = normalize_space(text)
        if not candidate:
            return None

        tb_match = TB_RE.search(candidate)
        if tb_match:
            try:
                return round(float(tb_match.group(1).replace(",", ".")), 2)
            except ValueError:
                return None

        gb_match = GB_RE.search(candidate)
        if gb_match:
            try:
                gb = float(gb_match.group(1))
                return round(gb / 1000.0, 2)
            except ValueError:
                return None
        return None

    def _score_price_candidate(self, text: str) -> tuple[float, float | None]:
        candidate = normalize_space(text).casefold()
        if not candidate:
            return 0.0, None

        score = 0.0

        include_hits = 0
        for term in self.settings.price_keywords_include:
            token = term.casefold().strip()
            if token and token in candidate:
                include_hits += 1
        score += min(0.30, include_hits * 0.08)

        if "ssd" in candidate:
            score += 0.25
        if "nvme" in candidate:
            score += 0.25
        if "m.2" in candidate or re.search(r"\bm2\b", candidate):
            score += 0.18
        if "pcie" in candidate:
            score += 0.08

        for term in self.settings.price_keywords_exclude:
            token = term.casefold().strip()
            if token and token in candidate:
                score -= 0.25

        capacity_tb = self._extract_capacity_tb(candidate)
        if capacity_tb is not None:
            tolerance = max(0.1, self.settings.price_capacity_soft_tolerance_tb)
            distance = abs(capacity_tb - self.settings.price_preferred_capacity_tb)
            capacity_score = max(0.0, 1.0 - (distance / tolerance))
            score += 0.22 * capacity_score
        else:
            score += 0.02

        score = max(0.0, min(1.0, score))
        return score, capacity_tb

    @staticmethod
    def _price_source_name(url: str) -> str:
        parsed = urlparse(url)
        host = normalize_key(parsed.netloc) or "unknown"
        return f"price_{host}"

    def _make_price_observation(
        self,
        *,
        source_url: str,
        title: str,
        price: float,
        currency: str,
        url: str,
        context_text: str,
    ) -> PriceObservation | None:
        title = normalize_space(title)
        if not title:
            return None

        score, capacity_tb = self._score_price_candidate(f"{title} {context_text}")
        if score < self.settings.price_relevance_threshold:
            return None

        if not url:
            url = source_url
        full_url = urljoin(source_url, url)

        return PriceObservation(
            source=self._price_source_name(source_url),
            query=self.settings.price_query_label,
            title=title,
            price=price,
            currency=currency.upper() or "PLN",
            url=full_url,
            capacity_tb=capacity_tb,
            relevance=score,
        ).normalized()

    @staticmethod
    def _walk_json_nodes(payload: Any) -> Iterable[dict[str, Any]]:
        if isinstance(payload, dict):
            yield payload
            for value in payload.values():
                yield from SourceClient._walk_json_nodes(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from SourceClient._walk_json_nodes(item)

    def _extract_price_observations_from_ld_json(
        self,
        soup: BeautifulSoup,
        source_url: str,
    ) -> list[PriceObservation]:
        observations: list[PriceObservation] = []
        dedupe: set[str] = set()

        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.string or script.get_text(" ", strip=True)
            raw = (raw or "").strip()
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            for node in self._walk_json_nodes(payload):
                node_type = node.get("@type", "")
                if isinstance(node_type, list):
                    node_types = [normalize_space(str(t)).casefold() for t in node_type]
                else:
                    node_types = [normalize_space(str(node_type)).casefold()]

                if "product" not in node_types:
                    continue

                title = normalize_space(str(node.get("name", "")))
                if not title:
                    continue

                offers = node.get("offers")
                offers_list: list[dict[str, Any]] = []
                if isinstance(offers, dict):
                    offers_list = [offers]
                elif isinstance(offers, list):
                    offers_list = [item for item in offers if isinstance(item, dict)]

                if not offers_list:
                    offers_list = [node]

                for offer in offers_list:
                    price_raw = offer.get("price", offer.get("lowPrice", node.get("price", node.get("lowPrice"))))
                    price = self._parse_price_amount(price_raw)
                    if price is None or price <= 0:
                        continue

                    currency = normalize_space(
                        str(offer.get("priceCurrency", node.get("priceCurrency", "PLN")))
                    ).upper() or "PLN"
                    offer_url = normalize_space(str(offer.get("url", node.get("url", source_url))))
                    context_text = normalize_space(
                        f"{node.get('description', '')} {node.get('sku', '')} {node.get('brand', '')}"
                    )
                    observation = self._make_price_observation(
                        source_url=source_url,
                        title=title,
                        price=price,
                        currency=currency,
                        url=offer_url,
                        context_text=context_text,
                    )
                    if observation is None:
                        continue

                    dedupe_key = f"{observation.item_key}|{observation.price:.2f}"
                    if dedupe_key in dedupe:
                        continue
                    dedupe.add(dedupe_key)
                    observations.append(observation)

        return observations

    def _extract_price_observations_from_dom(
        self,
        soup: BeautifulSoup,
        source_url: str,
    ) -> list[PriceObservation]:
        observations: list[PriceObservation] = []
        dedupe: set[str] = set()
        limit = max(20, self.settings.price_max_candidates_per_source * 5)

        for anchor in soup.select("a[href]"):
            href = normalize_space(anchor.get("href", ""))
            if not href:
                continue

            title = normalize_space(anchor.get_text(" ", strip=True))
            if len(title) < 8:
                continue

            container = anchor
            context_text = ""
            for _ in range(4):
                text_candidate = normalize_space(container.get_text(" ", strip=True))
                if text_candidate and PRICE_RE.search(text_candidate):
                    context_text = text_candidate
                    break
                parent = getattr(container, "parent", None)
                if parent is None:
                    break
                container = parent

            if not context_text:
                continue

            price_match = PRICE_RE.search(context_text)
            if not price_match:
                continue

            amount_raw, currency_raw = price_match.group(1), price_match.group(2)
            price = self._parse_price_amount(amount_raw)
            if price is None or price <= 0:
                continue

            currency = currency_raw.upper()
            if currency in {"ZŁ", "PLN"}:
                currency = "PLN"
            if currency == "$":
                currency = "USD"
            if currency == "€":
                currency = "EUR"

            observation = self._make_price_observation(
                source_url=source_url,
                title=title,
                price=price,
                currency=currency,
                url=href,
                context_text=context_text[:700],
            )
            if observation is None:
                continue

            dedupe_key = f"{observation.item_key}|{observation.price:.2f}"
            if dedupe_key in dedupe:
                continue
            dedupe.add(dedupe_key)
            observations.append(observation)
            if len(observations) >= limit:
                break

        return observations

    @staticmethod
    def _is_pcpartpicker_trends_source(url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.casefold()
        path = parsed.path.casefold()
        return "pcpartpicker.com" in host and "/trends/price/" in path

    @staticmethod
    def _looks_like_pcpartpicker_challenge(html: str) -> bool:
        body = (html or "").casefold()
        if "pcpartpicker is unavailable" in body:
            return True
        if "performing security verification" in body:
            return True
        if "/cdn-cgi/challenge-platform/" in body:
            return True
        return False

    @staticmethod
    def _decode_png_rgba(png_bytes: bytes) -> tuple[int, int, bytes] | None:
        signature = b"\x89PNG\r\n\x1a\n"
        if not png_bytes.startswith(signature):
            return None

        offset = len(signature)
        width = 0
        height = 0
        bit_depth = 0
        color_type = 0
        idat_chunks: list[bytes] = []

        while offset + 8 <= len(png_bytes):
            chunk_len = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
            chunk_type = png_bytes[offset + 4 : offset + 8]
            chunk_start = offset + 8
            chunk_end = chunk_start + chunk_len
            crc_end = chunk_end + 4
            if crc_end > len(png_bytes):
                return None
            chunk_data = png_bytes[chunk_start:chunk_end]

            if chunk_type == b"IHDR":
                if chunk_len < 13:
                    return None
                width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk_data[:10])
            elif chunk_type == b"IDAT":
                idat_chunks.append(chunk_data)
            elif chunk_type == b"IEND":
                break

            offset = crc_end

        if width <= 0 or height <= 0:
            return None
        if bit_depth != 8:
            return None
        if color_type not in {2, 6}:
            return None
        if not idat_chunks:
            return None

        bytes_per_px = 4 if color_type == 6 else 3
        stride = width * bytes_per_px
        expected_len = (stride + 1) * height
        try:
            decompressed = zlib.decompress(b"".join(idat_chunks))
        except zlib.error:
            return None
        if len(decompressed) < expected_len:
            return None

        rgba = bytearray(width * height * 4)
        prev = bytearray(stride)
        cursor = 0
        out_cursor = 0

        for _ in range(height):
            filter_type = decompressed[cursor]
            cursor += 1
            row = bytearray(decompressed[cursor : cursor + stride])
            cursor += stride

            if filter_type == 1:
                for i in range(bytes_per_px, stride):
                    row[i] = (row[i] + row[i - bytes_per_px]) & 0xFF
            elif filter_type == 2:
                for i in range(stride):
                    row[i] = (row[i] + prev[i]) & 0xFF
            elif filter_type == 3:
                for i in range(stride):
                    left = row[i - bytes_per_px] if i >= bytes_per_px else 0
                    up = prev[i]
                    row[i] = (row[i] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                for i in range(stride):
                    left = row[i - bytes_per_px] if i >= bytes_per_px else 0
                    up = prev[i]
                    up_left = prev[i - bytes_per_px] if i >= bytes_per_px else 0
                    p = left + up - up_left
                    pa = abs(p - left)
                    pb = abs(p - up)
                    pc = abs(p - up_left)
                    if pa <= pb and pa <= pc:
                        pred = left
                    elif pb <= pc:
                        pred = up
                    else:
                        pred = up_left
                    row[i] = (row[i] + pred) & 0xFF
            elif filter_type != 0:
                return None

            if color_type == 6:
                rgba[out_cursor : out_cursor + (width * 4)] = row
                out_cursor += width * 4
            else:
                for i in range(0, len(row), 3):
                    rgba[out_cursor] = row[i]
                    rgba[out_cursor + 1] = row[i + 1]
                    rgba[out_cursor + 2] = row[i + 2]
                    rgba[out_cursor + 3] = 255
                    out_cursor += 4
            prev = row

        return width, height, bytes(rgba)

    @staticmethod
    def _extract_trend_index_from_png(png_bytes: bytes) -> float | None:
        decoded = SourceClient._decode_png_rgba(png_bytes)
        if decoded is None:
            return None
        width, height, rgba = decoded

        if width < 60 or height < 30:
            return None

        x0 = max(0, int(width * 0.12))
        x1 = min(width - 1, int(width * 0.97))
        y0 = max(0, int(height * 0.08))
        y1 = min(height - 1, int(height * 0.94))
        if x1 <= x0 or y1 <= y0:
            return None

        w = width
        h = height
        mask = bytearray(w * h)

        def is_ink(x: int, y: int) -> bool:
            idx = (y * w + x) * 4
            r = rgba[idx]
            g = rgba[idx + 1]
            b = rgba[idx + 2]
            a = rgba[idx + 3]
            if a < 160:
                return False
            luma = (54 * r + 183 * g + 19 * b) // 256
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            sat = 0.0 if max_c == 0 else (max_c - min_c) / max_c
            return luma <= 95 or (sat >= 0.45 and luma <= 165)

        for y in range(y0, y1 + 1):
            row_offset = y * w
            for x in range(x0, x1 + 1):
                if is_ink(x, y):
                    mask[row_offset + x] = 1

        visited = bytearray(w * h)
        best_points: list[tuple[int, int]] = []
        best_score = -1.0
        min_span = max(20, int((x1 - x0 + 1) * 0.35))
        min_pixels = max(80, int((x1 - x0 + 1) * 1.1))

        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                pos = y * w + x
                if not mask[pos] or visited[pos]:
                    continue
                q: deque[tuple[int, int]] = deque([(x, y)])
                visited[pos] = 1
                points: list[tuple[int, int]] = []
                min_x = x
                max_x = x
                while q:
                    cx, cy = q.popleft()
                    points.append((cx, cy))
                    if cx < min_x:
                        min_x = cx
                    if cx > max_x:
                        max_x = cx
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if nx < x0 or nx > x1 or ny < y0 or ny > y1:
                            continue
                        npos = ny * w + nx
                        if visited[npos] or not mask[npos]:
                            continue
                        visited[npos] = 1
                        q.append((nx, ny))

                span = max_x - min_x + 1
                if span < min_span or len(points) < min_pixels:
                    continue
                score = span * 2.0 + len(points)
                if score > best_score:
                    best_score = score
                    best_points = points

        if not best_points:
            return None

        right_x = max(point[0] for point in best_points)
        tail_start = right_x - max(3, int((x1 - x0 + 1) * 0.04))
        tail_y = sorted(point[1] for point in best_points if point[0] >= tail_start)
        if not tail_y:
            return None
        y_current = float(tail_y[len(tail_y) // 2])

        y_norm = (y_current - y0) / max(1.0, float(y1 - y0))
        index = (1.0 - max(0.0, min(1.0, y_norm))) * 100.0
        return round(index, 2)

    @staticmethod
    def _title_from_trend_image_url(image_url: str) -> str:
        path = urlparse(image_url).path
        filename = path.rsplit("/", 1)[-1]
        stem = filename.removesuffix(".png")
        parts = [part for part in stem.split(".") if part]
        if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
            parts = parts[3:]
        if parts and re.fullmatch(r"[0-9a-f]{16,}", parts[-1]):
            parts = parts[:-1]
        if parts and parts[0].lower() in {"usd", "eur", "pln", "gbp"}:
            parts = parts[1:]
        return normalize_space(" ".join(parts).replace("-", " "))

    def _collect_pcpartpicker_trend_image_candidates(
        self,
        soup: BeautifulSoup,
        source_url: str,
    ) -> list[tuple[str, str]]:
        by_url: dict[str, str] = {}

        for img in soup.select("img[src]"):
            src = normalize_space(img.get("src", ""))
            if not src:
                continue
            if not PCPARTPICKER_TREND_IMG_RE.search(src):
                continue

            image_url = urljoin(source_url, src)
            hints: list[str] = []
            for attr in ("alt", "title", "data-title", "data-name"):
                text = normalize_space(str(img.get(attr, "")))
                if text:
                    hints.append(text)

            for parent in list(img.parents)[:6]:
                if parent is None:
                    continue
                for heading in parent.find_all(["h2", "h3", "h4", "h5", "figcaption"], recursive=False):
                    text = normalize_space(heading.get_text(" ", strip=True))
                    if text:
                        hints.append(text)
                sibling_heading = parent.find_previous_sibling(["h2", "h3", "h4", "h5"])
                if sibling_heading is not None:
                    text = normalize_space(sibling_heading.get_text(" ", strip=True))
                    if text:
                        hints.append(text)

            fallback_title = self._title_from_trend_image_url(image_url)
            if fallback_title:
                hints.append(fallback_title)

            title = ""
            for hint in hints:
                if len(hint) < 2:
                    continue
                title = hint
                if any(token in hint.casefold() for token in ("ssd", "nvme", "m.2", "m2", "tb", "pcie")):
                    break
            if not title:
                title = "PCPartPicker trend"

            by_url.setdefault(image_url, title)

        return [(title, image_url) for image_url, title in by_url.items()]

    def _extract_price_observations_from_pcpartpicker_trends(
        self,
        *,
        source_url: str,
        html: str,
        soup: BeautifulSoup | None = None,
    ) -> list[PriceObservation]:
        if soup is None:
            soup = BeautifulSoup(html, "html.parser")

        candidates = self._collect_pcpartpicker_trend_image_candidates(soup, source_url)
        if not candidates and self.settings.price_trend_image_urls:
            for image_url in self.settings.price_trend_image_urls:
                title = self._title_from_trend_image_url(image_url) or "PCPartPicker trend"
                candidates.append((title, image_url))

        if not candidates:
            if self._looks_like_pcpartpicker_challenge(html):
                logger.warning(
                    "price source %s blocked by anti-bot; set PRICE_TREND_IMAGE_URLS with direct chart URLs",
                    source_url,
                )
            return []

        observations: list[PriceObservation] = []
        dedupe: set[str] = set()
        limit = max(5, self.settings.price_max_candidates_per_source * 2)

        for title, image_url in candidates:
            if len(observations) >= limit:
                break
            try:
                response = self.session.get(
                    image_url,
                    timeout=self.settings.request_timeout_seconds,
                )
                response.raise_for_status()
                trend_index = self._extract_trend_index_from_png(response.content)
                if trend_index is None:
                    continue
                observation = self._make_price_observation(
                    source_url=source_url,
                    title=title,
                    price=trend_index,
                    currency="IDX",
                    url=image_url,
                    context_text=f"{title} pcpartpicker trend image",
                )
                if observation is None:
                    continue
                dedupe_key = f"{observation.item_key}|{observation.price:.2f}"
                if dedupe_key in dedupe:
                    continue
                dedupe.add(dedupe_key)
                observations.append(observation)
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to parse PCPartPicker trend image %s: %s", image_url, exc)

        return observations

    def fetch_price_observations(self) -> list[PriceObservation]:
        if not self.settings.enable_price_monitoring:
            return []
        if not self.settings.price_source_urls:
            return []

        all_observations: list[PriceObservation] = []
        for source_url in self.settings.price_source_urls:
            try:
                html = self._get(source_url)
                soup = BeautifulSoup(html, "html.parser")
                observations: list[PriceObservation] = []
                if self._is_pcpartpicker_trends_source(source_url):
                    observations.extend(
                        self._extract_price_observations_from_pcpartpicker_trends(
                            source_url=source_url,
                            html=html,
                            soup=soup,
                        )
                    )

                if len(observations) < self.settings.price_max_candidates_per_source:
                    observations.extend(self._extract_price_observations_from_ld_json(soup, source_url))

                if len(observations) < self.settings.price_max_candidates_per_source:
                    observations.extend(self._extract_price_observations_from_dom(soup, source_url))

                by_item: dict[str, PriceObservation] = {}
                for observation in observations:
                    existing = by_item.get(observation.item_key)
                    if existing is None:
                        by_item[observation.item_key] = observation
                        continue
                    if observation.price < existing.price:
                        by_item[observation.item_key] = observation
                    elif observation.price == existing.price and observation.relevance > existing.relevance:
                        by_item[observation.item_key] = observation
                    elif (
                        observation.price == existing.price
                        and observation.relevance == existing.relevance
                        and short_hash(observation.url) < short_hash(existing.url)
                    ):
                        by_item[observation.item_key] = observation

                ranked = sorted(
                    by_item.values(),
                    key=lambda item: (-item.relevance, item.price, item.title.casefold()),
                )
                trimmed = ranked[: self.settings.price_max_candidates_per_source]
                logger.info("price source %s: parsed %d candidates", source_url, len(trimmed))
                all_observations.extend(trimmed)
            except Exception as exc:  # noqa: BLE001
                logger.exception("price source %s failed: %s", source_url, exc)

        return all_observations

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
