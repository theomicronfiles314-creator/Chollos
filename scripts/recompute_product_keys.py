"""Recalcula product_key para todos los anuncios ya guardados.

Útil después de cambiar la lógica de src/normalizer.py: sin esto, los
anuncios ya guardados se quedan con la clave antigua y no agrupan bien con
los nuevos.

Uso: python -m scripts.recompute_product_keys
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_DB_PATH
from src.db import connect
from src.normalizer import product_key


def main() -> None:
    with connect(DEFAULT_DB_PATH) as conn:
        rows = conn.execute("SELECT id, category_id, title FROM listings").fetchall()
        updated = 0
        for row in rows:
            new_key = product_key(row["category_id"], row["title"])
            conn.execute(
                "UPDATE listings SET product_key = ? WHERE id = ?",
                (new_key, row["id"]),
            )
            updated += 1
        conn.commit()

        distinct_before = len(rows)
        distinct_after = conn.execute(
            "SELECT COUNT(DISTINCT product_key) c FROM listings"
        ).fetchone()["c"]
        with_5_plus = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT product_key FROM listings GROUP BY product_key HAVING COUNT(*) >= 5)"
        ).fetchone()["c"]

        print(f"Anuncios recalculados: {updated}")
        print(f"product_key distintos ahora: {distinct_after}")
        print(f"product_key con >=5 muestras: {with_5_plus}")


if __name__ == "__main__":
    main()
