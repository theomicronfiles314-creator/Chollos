"""Resumen rápido de lo que lleva viendo el bot.

Uso: python -m scripts.stats
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_DB_PATH
from src.db import connect

RARITY_EMOJI = {"legendario": "🟡", "epico": "🟣", "raro": "🔵", "comun": "⚪"}


def main() -> None:
    with connect(DEFAULT_DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
        chollos = conn.execute("SELECT COUNT(*) c FROM listings WHERE is_chollo = 1").fetchone()["c"]

        print(f"Anuncios guardados: {total}")
        print(f"Chollos detectados: {chollos}")
        print()

        print("-- Por búsqueda --")
        for row in conn.execute(
            """
            SELECT search_name, COUNT(*) total, SUM(is_chollo) chollos
            FROM listings GROUP BY search_name ORDER BY total DESC
            """
        ):
            print(f"  {row['search_name']:<28} {row['total']:>4} anuncios, {row['chollos'] or 0} chollos")
        print()

        print("-- Por rareza --")
        for row in conn.execute(
            """
            SELECT rarity, COUNT(*) c FROM listings
            WHERE is_chollo = 1 GROUP BY rarity ORDER BY c DESC
            """
        ):
            emoji = RARITY_EMOJI.get(row["rarity"], "")
            print(f"  {emoji} {row['rarity']:<12} {row['c']}")
        print()

        print("-- Últimos 10 chollos --")
        for row in conn.execute(
            """
            SELECT title, price, currency, discount_pct, margin_eur, rarity, url, first_seen_at
            FROM listings WHERE is_chollo = 1
            ORDER BY first_seen_at DESC LIMIT 10
            """
        ):
            emoji = RARITY_EMOJI.get(row["rarity"], "")
            print(
                f"  {emoji} {row['title'][:45]:<45} {row['price']:.0f}{row['currency']} "
                f"(-{row['discount_pct']:.0f}%, +{row['margin_eur']:.0f}{row['currency']}) "
                f"{row['first_seen_at'][:16]}"
            )
            print(f"      {row['url']}")

        print()
        print("-- Grupos de producto más cerca de tener histórico fiable --")
        for row in conn.execute(
            """
            SELECT product_key, COUNT(*) c FROM listings
            GROUP BY product_key ORDER BY c DESC LIMIT 10
            """
        ):
            print(f"  {row['c']:>3}  {row['product_key']}")


if __name__ == "__main__":
    main()
