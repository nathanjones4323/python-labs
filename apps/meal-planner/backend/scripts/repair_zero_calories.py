#!/usr/bin/env python3
"""
Two-phase repair for foods with zero calories in the local database.

Phase 1 (fast, no API): Foods with zero calories but non-zero macros get their
calories recalculated via the Atwater formula and written back to the JSONB.

Phase 2 (slower, API): Foods where calories AND all macros are zero are
re-fetched from the USDA detail API (/food/{fdcId}), which returns the full
nutrient list rather than the truncated search-API subset.

Safe to re-run — Phase 1 is a pure SQL UPDATE; Phase 2 uses ON CONFLICT DO UPDATE.

Usage:
    uv run python scripts/repair_zero_calories.py
"""

import json
import sys
import time
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from meal_planner.config import DATABASE_URL, USDA_API_KEY, USDA_BASE_URL
from meal_planner.services.usda import NUTRIENT_MAP

DB_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


def phase0_kj_fix(conn) -> int:
    """Fix foods where stored calories are ~4x the macro-derived value.

    SR Legacy nutrient ID 1062 is stored in kJ, but the NUTRIENT_MAP
    incorrectly treated it as kcal. 1 kcal ≈ 4.184 kJ, so affected foods show
    ~3700 kcal instead of ~900 kcal for pure fats. This replaces stored
    calories with the Atwater macro calculation for any food where stored
    calories exceed 2× the macro-derived value (and macros are non-trivial).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE foods
            SET nutrients = jsonb_set(
              nutrients,
              '{calories}',
              to_jsonb(
                ROUND(
                  COALESCE((nutrients->>'protein_g')::numeric, 0) * 4
                + COALESCE((nutrients->>'carbs_g')::numeric,   0) * 4
                + COALESCE((nutrients->>'fat_g')::numeric,     0) * 9,
                4)
              )
            )
            WHERE COALESCE((nutrients->>'calories')::numeric, 0) > 0
              AND (
                COALESCE((nutrients->>'protein_g')::numeric, 0) * 4
              + COALESCE((nutrients->>'carbs_g')::numeric,   0) * 4
              + COALESCE((nutrients->>'fat_g')::numeric,     0) * 9
              ) > 0
              AND COALESCE((nutrients->>'calories')::numeric, 0) >
                  2 * (
                    COALESCE((nutrients->>'protein_g')::numeric, 0) * 4
                  + COALESCE((nutrients->>'carbs_g')::numeric,   0) * 4
                  + COALESCE((nutrients->>'fat_g')::numeric,     0) * 9
                  )
            """
        )
        updated = cur.rowcount
    conn.commit()
    return updated


def phase1_macro_fix(conn) -> int:
    """Recalculate calories from macros for zero-calorie foods that have macro data."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE foods
            SET nutrients = jsonb_set(
              nutrients,
              '{calories}',
              to_jsonb(
                ROUND(
                  (nutrients->>'protein_g')::numeric * 4
                + (nutrients->>'carbs_g')::numeric  * 4
                + (nutrients->>'fat_g')::numeric    * 9,
                4)
              )
            )
            WHERE (nutrients->>'calories')::numeric = 0
              AND ((nutrients->>'fat_g')::numeric     > 0
                OR (nutrients->>'protein_g')::numeric > 0
                OR (nutrients->>'carbs_g')::numeric   > 0)
            """
        )
        updated = cur.rowcount
    conn.commit()
    return updated


def _parse_detail_nutrients(food_nutrients: list[dict]) -> dict:
    data: dict = {}
    for n in food_nutrients:
        nutrient = n.get("nutrient", n)
        nid = nutrient.get("id") or nutrient.get("nutrientId")
        raw = n.get("amount")
        if raw is None:
            raw = n.get("value")
        amount = raw if raw is not None else 0.0
        if nid in NUTRIENT_MAP:
            field = NUTRIENT_MAP[nid]
            val = round(float(amount), 4)
            if field == "calories":
                data["calories"] = max(data.get("calories", 0.0), val)
            else:
                data[field] = val
    if not data.get("calories") and (data.get("protein_g") or data.get("carbs_g") or data.get("fat_g")):
        data["calories"] = round(
            data.get("protein_g", 0) * 4
            + data.get("carbs_g", 0) * 4
            + data.get("fat_g", 0) * 9,
            4,
        )
    return data


def phase2_detail_fetch(conn) -> tuple[int, int]:
    """Re-fetch foods with zero calories and zero macros from the USDA detail API."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fdc_id, description FROM foods
            WHERE (nutrients->>'calories')::numeric = 0
              AND (nutrients->>'fat_g')::numeric     = 0
              AND (nutrients->>'protein_g')::numeric = 0
              AND (nutrients->>'carbs_g')::numeric   = 0
            ORDER BY fdc_id
            """
        )
        targets = cur.fetchall()

    if not targets:
        return 0, 0

    print(f"  {len(targets)} foods need detail API re-fetch...")
    updated = 0
    failed = 0

    with httpx.Client(timeout=30.0) as client:
        for i, (fdc_id, desc) in enumerate(targets, 1):
            if USDA_API_KEY == "DEMO_KEY" and i > 1:
                time.sleep(2)  # DEMO_KEY: 30 req/hr; brief pause avoids 429s for small batches

            resp = client.get(
                f"{USDA_BASE_URL}/food/{fdc_id}",
                params={"api_key": USDA_API_KEY},
            )
            if resp.status_code == 429:
                print(f"  Rate limited at food {i}/{len(targets)} — waiting 60s...")
                time.sleep(60)
                resp = client.get(
                    f"{USDA_BASE_URL}/food/{fdc_id}",
                    params={"api_key": USDA_API_KEY},
                )
            if resp.status_code == 404:
                print(f"  WARN: fdc_id {fdc_id} not found in USDA ({desc[:40]})")
                failed += 1
                continue
            try:
                resp.raise_for_status()
            except Exception as e:
                print(f"  WARN: fdc_id {fdc_id} error: {e}")
                failed += 1
                continue

            item = resp.json()
            nutrients = _parse_detail_nutrients(item.get("foodNutrients", []))
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE foods SET nutrients = %s, cached_at = NOW()
                    WHERE fdc_id = %s
                    """,
                    (json.dumps(nutrients), fdc_id),
                )
            conn.commit()
            updated += 1
            print(f"  {i}/{len(targets)}: {desc[:50]}  cal={nutrients.get('calories', 0)}\r", end="", flush=True)

    print()
    return updated, failed


def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        print("Phase 0: Fix kJ-inflated calories (SR Legacy ID 1062 stored in kJ, not kcal)...")
        n = phase0_kj_fix(conn)
        print(f"  Updated {n} records.")

        print("Phase 1: Atwater macro recalculation for zero-calorie foods with macros...")
        n = phase1_macro_fix(conn)
        print(f"  Updated {n} records.")

        print("Phase 2: Detail API re-fetch for fully-empty records...")
        updated, failed = phase2_detail_fetch(conn)
        print(f"  Re-fetched {updated} records; {failed} failed (see warnings above).")

    print("\nDone. Run audit_nutrition.py to verify.")


if __name__ == "__main__":
    main()
