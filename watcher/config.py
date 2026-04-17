from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_BILETOMAT_URLS = {
    "dziady": "https://biletomat.pl/wydarzenia/dziady-15362",
    "wesele": "https://biletomat.pl/wydarzenia/wesele-slowacki-22040",
}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    monitored_plays: list[str]
    request_timeout_seconds: int
    user_agent: str
    check_interval_seconds: int
    state_db_path: str
    theater_repertuar_url: str
    theater_ticket_page_template: str
    play_slug_map: dict[str, str]
    enable_biletomat: bool
    biletomat_urls: dict[str, str]
    enable_facebook: bool
    facebook_graph_base_url: str
    facebook_graph_version: str
    facebook_access_token: str
    facebook_page_id: str
    facebook_group_id: str
    facebook_keywords_include: list[str]
    facebook_keywords_exclude: list[str]
    facebook_max_posts: int
    enable_price_monitoring: bool
    price_source_urls: list[str]
    price_trend_image_urls: list[str]
    price_query_label: str
    price_keywords_include: list[str]
    price_keywords_exclude: list[str]
    price_preferred_capacity_tb: float
    price_capacity_soft_tolerance_tb: float
    price_relevance_threshold: float
    price_max_candidates_per_source: int
    price_min_observations_for_trend: int
    price_trend_window_size: int
    price_drop_alert_percent: float
    price_rise_alert_percent: float
    price_alert_cooldown_hours: int
    smoke_notify_on_success: bool
    smoke_notify_on_failure: bool
    ntfy_server: str
    ntfy_topic: str
    ntfy_token: str
    ntfy_username: str
    ntfy_password: str
    ntfy_priority_alerts: str
    ntfy_priority_price_alerts: str
    ntfy_priority_smoke_success: str
    ntfy_priority_smoke_failure: str
    ntfy_tags_alerts: list[str]
    ntfy_tags_price_alerts: list[str]
    ntfy_tags_smoke: list[str]
    signal_cli_path: str
    signal_account: str
    signal_recipients: list[str]
    signal_timeout_seconds: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    email_from: str
    email_to: list[str]
    email_subject_prefix: str
    email_fallback_on_ticket_alerts: bool
    email_fallback_on_price_alerts: bool
    telegram_bot_token: str
    telegram_chat_id: str

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_server and self.ntfy_topic)

    @property
    def signal_enabled(self) -> bool:
        return bool(self.signal_account and self.signal_recipients)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.email_from and self.email_to)

    @property
    def facebook_ready(self) -> bool:
        has_target = bool(self.facebook_page_id or self.facebook_group_id)
        return self.enable_facebook and bool(self.facebook_access_token) and has_target


def load_settings() -> Settings:
    load_dotenv()
    plays = _list_env("MONITORED_PLAYS", ["Dziady", "Wesele"])

    play_slug_map: dict[str, str] = {}
    for play in plays:
        env_key = f"PLAY_SLUG_{play.upper().replace(' ', '_')}"
        slug = os.getenv(env_key, "").strip()
        if slug:
            play_slug_map[play] = slug
        else:
            play_slug_map[play] = play.casefold().replace(" ", "-")

    biletomat_urls: dict[str, str] = {}
    for play in plays:
        env_key = f"BILETOMAT_URL_{play.upper().replace(' ', '_')}"
        url = os.getenv(env_key, "").strip()
        if url:
            biletomat_urls[play] = url
            continue
        default_url = DEFAULT_BILETOMAT_URLS.get(play.casefold())
        if default_url:
            biletomat_urls[play] = default_url

    return Settings(
        monitored_plays=plays,
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        user_agent=os.getenv(
            "USER_AGENT",
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        ),
        check_interval_seconds=int(os.getenv("CHECK_INTERVAL_SECONDS", "180")),
        state_db_path=os.getenv("STATE_DB_PATH", "./data/state.sqlite3"),
        theater_repertuar_url=os.getenv(
            "THEATER_REPERTUAR_URL",
            "https://teatrwkrakowie.pl/repertuar",
        ),
        theater_ticket_page_template=os.getenv(
            "THEATER_TICKET_PAGE_TEMPLATE",
            "https://bilety.teatrwkrakowie.pl/index.php/bilety/{slug}",
        ),
        play_slug_map=play_slug_map,
        enable_biletomat=_bool_env("ENABLE_BILETOMAT", True),
        biletomat_urls=biletomat_urls,
        enable_facebook=_bool_env("ENABLE_FACEBOOK", False),
        facebook_graph_base_url=os.getenv("FACEBOOK_GRAPH_BASE_URL", "https://graph.facebook.com").rstrip("/"),
        facebook_graph_version=os.getenv("FACEBOOK_GRAPH_VERSION", "v25.0"),
        facebook_access_token=os.getenv("FACEBOOK_ACCESS_TOKEN", "").strip(),
        facebook_page_id=os.getenv("FACEBOOK_PAGE_ID", "").strip(),
        facebook_group_id=os.getenv("FACEBOOK_GROUP_ID", "").strip(),
        facebook_keywords_include=_list_env("FACEBOOK_KEYWORDS_INCLUDE", plays),
        facebook_keywords_exclude=_list_env("FACEBOOK_KEYWORDS_EXCLUDE", []),
        facebook_max_posts=int(os.getenv("FACEBOOK_MAX_POSTS", "25")),
        enable_price_monitoring=_bool_env("ENABLE_PRICE_MONITORING", True),
        price_source_urls=_list_env("PRICE_SOURCE_URLS", []),
        price_trend_image_urls=_list_env("PRICE_TREND_IMAGE_URLS", []),
        price_query_label=os.getenv("PRICE_QUERY_LABEL", "NVMe SSD M.2").strip(),
        price_keywords_include=_list_env(
            "PRICE_KEYWORDS_INCLUDE",
            ["ssd", "nvme", "m.2", "m2", "pcie"],
        ),
        price_keywords_exclude=_list_env(
            "PRICE_KEYWORDS_EXCLUDE",
            ["adapter", "obudowa", "kabel", "heatsink", "radiator"],
        ),
        price_preferred_capacity_tb=_float_env("PRICE_PREFERRED_CAPACITY_TB", 4.0),
        price_capacity_soft_tolerance_tb=_float_env("PRICE_CAPACITY_SOFT_TOLERANCE_TB", 2.0),
        price_relevance_threshold=_float_env("PRICE_RELEVANCE_THRESHOLD", 0.45),
        price_max_candidates_per_source=int(os.getenv("PRICE_MAX_CANDIDATES_PER_SOURCE", "25")),
        price_min_observations_for_trend=int(os.getenv("PRICE_MIN_OBSERVATIONS_FOR_TREND", "4")),
        price_trend_window_size=int(os.getenv("PRICE_TREND_WINDOW_SIZE", "8")),
        price_drop_alert_percent=_float_env("PRICE_DROP_ALERT_PERCENT", 5.0),
        price_rise_alert_percent=_float_env("PRICE_RISE_ALERT_PERCENT", 8.0),
        price_alert_cooldown_hours=int(os.getenv("PRICE_ALERT_COOLDOWN_HOURS", "24")),
        smoke_notify_on_success=_bool_env("SMOKE_NOTIFY_ON_SUCCESS", False),
        smoke_notify_on_failure=_bool_env("SMOKE_NOTIFY_ON_FAILURE", True),
        ntfy_server=os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/"),
        ntfy_topic=os.getenv("NTFY_TOPIC", "").strip(),
        ntfy_token=os.getenv("NTFY_TOKEN", "").strip(),
        ntfy_username=os.getenv("NTFY_USERNAME", "").strip(),
        ntfy_password=os.getenv("NTFY_PASSWORD", "").strip(),
        ntfy_priority_alerts=os.getenv("NTFY_PRIORITY_ALERTS", "high").strip(),
        ntfy_priority_price_alerts=os.getenv("NTFY_PRIORITY_PRICE_ALERTS", "high").strip(),
        ntfy_priority_smoke_success=os.getenv("NTFY_PRIORITY_SMOKE_SUCCESS", "default").strip(),
        ntfy_priority_smoke_failure=os.getenv("NTFY_PRIORITY_SMOKE_FAILURE", "urgent").strip(),
        ntfy_tags_alerts=_list_env("NTFY_TAGS_ALERTS", ["ticket", "theatre"]),
        ntfy_tags_price_alerts=_list_env("NTFY_TAGS_PRICE_ALERTS", ["price", "ssd", "nvme"]),
        ntfy_tags_smoke=_list_env("NTFY_TAGS_SMOKE", ["warning", "monitoring"]),
        signal_cli_path=os.getenv("SIGNAL_CLI_PATH", "signal-cli").strip(),
        signal_account=os.getenv("SIGNAL_ACCOUNT", "").strip(),
        signal_recipients=_list_env("SIGNAL_RECIPIENTS", []),
        signal_timeout_seconds=int(os.getenv("SIGNAL_TIMEOUT_SECONDS", "30")),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_use_tls=_bool_env("SMTP_USE_TLS", True),
        email_from=os.getenv("EMAIL_FROM", "").strip(),
        email_to=_list_env("EMAIL_TO", []),
        email_subject_prefix=os.getenv("EMAIL_SUBJECT_PREFIX", "[tracker]").strip(),
        email_fallback_on_ticket_alerts=_bool_env("EMAIL_FALLBACK_ON_TICKET_ALERTS", False),
        email_fallback_on_price_alerts=_bool_env("EMAIL_FALLBACK_ON_PRICE_ALERTS", True),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    )
