from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SEARCHES_PATH = ROOT_DIR / "config" / "searches.yaml"
DEFAULT_DB_PATH = ROOT_DIR / "data" / "wallapop.db"


@dataclass
class SearchConfig:
    name: str
    keywords: str
    category_id: int | None = None
    min_price: float | None = None
    max_price: float | None = None
    order_by: str = "newest"
    min_year: int | None = None
    max_km: int | None = None
    source: str = "wallapop"  # "wallapop" | "milanuncios" | "autoscout24"


@dataclass
class Settings:
    latitude: float
    longitude: float
    interval_minutes: int
    discount_threshold_pct: float
    price_percentile: int
    min_samples_for_reference: int
    max_pages_per_search: int
    request_delay_range: tuple[float, float]
    rarity_raro_pct: float = 45
    rarity_epico_pct: float = 60
    rarity_legendario_pct: float = 75
    reference_window_days: int = 60
    concurrent_searches: int = 3
    circuit_breaker_failures: int = 5
    heartbeat_enabled: bool = True
    min_margin_eur: float = 8
    scam_discount_threshold_pct: float = 85
    max_reference_cv: float = 0.6
    seller_trust_check_enabled: bool = True
    seller_min_account_age_days: float = 14
    seller_min_reviews: float = 1
    seller_min_avg_rating: float = 3.5


@dataclass
class AppConfig:
    settings: Settings
    searches: list[SearchConfig] = field(default_factory=list)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    db_path: Path = DEFAULT_DB_PATH


def load_config(searches_path: Path | str = DEFAULT_SEARCHES_PATH) -> AppConfig:
    load_dotenv(ROOT_DIR / ".env")

    with open(searches_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw_settings = raw.get("settings", {})
    delay_range = raw_settings.get("request_delay_range", [2, 5])

    settings = Settings(
        latitude=raw_settings["latitude"],
        longitude=raw_settings["longitude"],
        interval_minutes=raw_settings.get("interval_minutes", 15),
        discount_threshold_pct=raw_settings.get("discount_threshold_pct", 30),
        price_percentile=raw_settings.get("price_percentile", 50),
        min_samples_for_reference=raw_settings.get("min_samples_for_reference", 5),
        max_pages_per_search=raw_settings.get("max_pages_per_search", 3),
        request_delay_range=(delay_range[0], delay_range[1]),
        rarity_raro_pct=raw_settings.get("rarity_raro_pct", 45),
        rarity_epico_pct=raw_settings.get("rarity_epico_pct", 60),
        rarity_legendario_pct=raw_settings.get("rarity_legendario_pct", 75),
        reference_window_days=raw_settings.get("reference_window_days", 60),
        concurrent_searches=raw_settings.get("concurrent_searches", 3),
        circuit_breaker_failures=raw_settings.get("circuit_breaker_failures", 5),
        heartbeat_enabled=raw_settings.get("heartbeat_enabled", True),
        min_margin_eur=raw_settings.get("min_margin_eur", 8),
        scam_discount_threshold_pct=raw_settings.get("scam_discount_threshold_pct", 85),
        max_reference_cv=raw_settings.get("max_reference_cv", 0.6),
        seller_trust_check_enabled=raw_settings.get("seller_trust_check_enabled", True),
        seller_min_account_age_days=raw_settings.get("seller_min_account_age_days", 14),
        seller_min_reviews=raw_settings.get("seller_min_reviews", 1),
        seller_min_avg_rating=raw_settings.get("seller_min_avg_rating", 3.5),
    )

    searches = [
        SearchConfig(
            name=s["name"],
            keywords=s["keywords"],
            category_id=s.get("category_id"),
            min_price=s.get("min_price"),
            max_price=s.get("max_price"),
            order_by=s.get("order_by", "newest"),
            min_year=s.get("min_year"),
            max_km=s.get("max_km"),
            source=s.get("source", "wallapop"),
        )
        for s in raw.get("searches", [])
    ]

    return AppConfig(
        settings=settings,
        searches=searches,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        db_path=DEFAULT_DB_PATH,
    )
