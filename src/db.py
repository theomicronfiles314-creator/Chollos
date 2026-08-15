from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .wallapop_client import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    search_name TEXT NOT NULL,
    product_key TEXT NOT NULL,
    category_id INTEGER,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    url TEXT NOT NULL,
    city TEXT,
    region TEXT,
    wallapop_created_at TEXT,
    first_seen_at TEXT NOT NULL,
    is_chollo INTEGER NOT NULL DEFAULT 0,
    discount_pct REAL,
    reference_price REAL,
    notified INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_listings_product_key ON listings (product_key);
CREATE INDEX IF NOT EXISTS idx_listings_search_name ON listings (search_name);
"""

# Columnas añadidas después de la creación inicial de la tabla: ALTER TABLE
# no admite "IF NOT EXISTS" en SQLite, así que se intentan y se ignora el
# error si ya existen (bases de datos creadas antes de este cambio).
MIGRATIONS = [
    "ALTER TABLE listings ADD COLUMN margin_eur REAL",
    "ALTER TABLE listings ADD COLUMN rarity TEXT",
]


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Cada hilo del pipeline abre su propia conexión (sqlite3 no permite
    # compartir una entre hilos), así que lo que realmente evita que unos
    # se bloqueen a otros es WAL + busy_timeout. Importante: busy_timeout
    # va ANTES que journal_mode=WAL — si dos conexiones intentan activar
    # WAL a la vez (p.ej. primera vez que se crea la BD, con varios hilos
    # arrancando en paralelo) la que llega segunda necesita poder esperar
    # en vez de fallar al instante con "database is locked".
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # columna ya existente
    conn.commit()
    return conn


@contextmanager
def connect(db_path: Path):
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def listing_exists(conn: sqlite3.Connection, listing_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return row is not None


def get_reference_prices(
    conn: sqlite3.Connection,
    product_key: str,
    window_days: int | None = 60,
    limit: int = 500,
) -> list[float]:
    """Precios recientes de `product_key`, para calcular una mediana que
    refleje el mercado actual y no precios de hace meses (que pueden ya no
    ser representativos). `window_days=None` para no filtrar por fecha."""
    if window_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        rows = conn.execute(
            """
            SELECT price FROM listings
            WHERE product_key = ? AND first_seen_at >= ?
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (product_key, cutoff, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT price FROM listings
            WHERE product_key = ?
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (product_key, limit),
        ).fetchall()
    return [r["price"] for r in rows]


def insert_listing(
    conn: sqlite3.Connection,
    listing: Listing,
    search_name: str,
    product_key: str,
    is_chollo: bool,
    discount_pct: float | None,
    reference_price: float | None,
    margin_eur: float | None = None,
    rarity: str | None = None,
    notified: bool = False,
) -> bool:
    """Inserta el anuncio. Devuelve False si ya existía (p.ej. dos
    búsquedas que se solapan encontrando el mismo id a la vez bajo
    concurrencia) en vez de lanzar un error de clave duplicada."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO listings (
            id, search_name, product_key, category_id, title, price, currency,
            url, city, region, wallapop_created_at, first_seen_at,
            is_chollo, discount_pct, reference_price, margin_eur, rarity, notified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing.id,
            search_name,
            product_key,
            listing.category_id,
            listing.title,
            listing.price,
            listing.currency,
            listing.url,
            listing.city,
            listing.region,
            listing.created_at.isoformat() if listing.created_at else None,
            datetime.now(timezone.utc).isoformat(),
            1 if is_chollo else 0,
            discount_pct,
            reference_price,
            margin_eur,
            rarity,
            1 if notified else 0,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_notified(conn: sqlite3.Connection, listing_id: str) -> None:
    conn.execute("UPDATE listings SET notified = 1 WHERE id = ?", (listing_id,))
    conn.commit()
