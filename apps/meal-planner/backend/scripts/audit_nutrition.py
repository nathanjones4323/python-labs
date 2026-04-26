#!/usr/bin/env python3
"""
Read-only nutrition data quality audit.

Connects to the local foods table and reports on data gaps:
  - Zero-calorie records
  - Empty nutrient dicts
  - Records where stored calories diverge from macro-derived calories

Usage:
    uv run python scripts/audit_nutrition.py
"""

import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from meal_planner.config import DATABASE_URL

DB_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


def run(conn) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM foods")
        total = cur.fetchone()["n"]
        print(f"Total foods: {total:,}")

        cur.execute("SELECT COUNT(*) AS n FROM foods WHERE nutrients = '{}'")
        empty = cur.fetchone()["n"]
        print(f"Empty nutrients ({{}}): {empty:,}  ({empty/total*100:.1f}%)")

        cur.execute(
            "SELECT COUNT(*) AS n FROM foods WHERE (nutrients->>'calories')::numeric = 0"
        )
        zero_cal = cur.fetchone()["n"]
        print(f"Zero calories: {zero_cal:,}  ({zero_cal/total*100:.1f}%)")

        cur.execute(
            """
            SELECT COUNT(*) AS n FROM foods
            WHERE (nutrients->>'calories')::numeric = 0
              AND ((nutrients->>'fat_g')::numeric > 0
                OR (nutrients->>'protein_g')::numeric > 0
                OR (nutrients->>'carbs_g')::numeric > 0)
            """
        )
        zero_cal_with_macros = cur.fetchone()["n"]
        print(
            f"  └─ zero calories but non-zero macros (fixable via Atwater): {zero_cal_with_macros:,}"
        )

        cur.execute(
            """
            SELECT COUNT(*) AS n FROM foods
            WHERE (nutrients->>'calories')::numeric = 0
              AND (nutrients->>'fat_g')::numeric = 0
              AND (nutrients->>'protein_g')::numeric = 0
              AND (nutrients->>'carbs_g')::numeric = 0
            """
        )
        fully_empty = cur.fetchone()["n"]
        print(f"  └─ zero calories AND zero macros (need detail API re-fetch): {fully_empty:,}")

        print()
        print("Top 30 foods where |stored_cal - macro_cal| > 50 kcal:")
        print(f"  {'fdc_id':<10} {'stored':>7} {'macro':>7} {'delta':>7} {'type':<12} description")
        print("  " + "-" * 80)

        cur.execute(
            """
            SELECT
              fdc_id,
              description,
              data_type,
              ROUND(COALESCE((nutrients->>'calories')::numeric, 0), 1)   AS stored_cal,
              ROUND(
                COALESCE((nutrients->>'protein_g')::numeric, 0) * 4
              + COALESCE((nutrients->>'carbs_g')::numeric,   0) * 4
              + COALESCE((nutrients->>'fat_g')::numeric,     0) * 9, 1)  AS macro_cal,
              ABS(
                COALESCE((nutrients->>'calories')::numeric, 0) -
                (  COALESCE((nutrients->>'protein_g')::numeric, 0) * 4
                 + COALESCE((nutrients->>'carbs_g')::numeric,   0) * 4
                 + COALESCE((nutrients->>'fat_g')::numeric,     0) * 9)
              )                                                           AS delta
            FROM foods
            WHERE COALESCE((nutrients->>'calories')::numeric, 0) > 0
            ORDER BY delta DESC
            LIMIT 30
            """
        )
        rows = cur.fetchall()
        for r in rows:
            if r["delta"] < 50:
                break
            print(
                f"  {r['fdc_id']:<10} {r['stored_cal']:>7} {r['macro_cal']:>7} {r['delta']:>7.0f}"
                f"  {(r['data_type'] or ''):<12} {r['description'][:50]}"
            )
        if not rows or rows[0]["delta"] < 50:
            print("  (none — all stored calories within 50 kcal of macro-derived value)")


def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        run(conn)


if __name__ == "__main__":
    main()
