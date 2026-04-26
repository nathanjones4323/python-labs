#!/usr/bin/env python3
"""
One-shot USDA FoodData Central ingestion script.

Fetches all Foundation Foods and SR Legacy items via the USDA search API
(paginated) and bulk-inserts them into the local `foods` table.

Safe to re-run — uses ON CONFLICT DO UPDATE.

Usage:
    uv run python scripts/ingest_usda.py

Fetches ~8,200 foods in ~42 API requests. Takes 2–5 minutes.
Set USDA_API_KEY in your .env for higher rate limits (1,000 req/hr vs 30/hr
for DEMO_KEY). Sign up free at https://fdc.nal.usda.gov/api-key-signup.html

USDA releases quarterly updates. Re-run this script to keep data current.
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

PAGE_SIZE = 200  # max allowed by USDA API
BATCH_SIZE = 200
DATA_TYPES = ["Foundation", "SR Legacy"]


def _parse_nutrients(food_nutrients: list[dict]) -> dict:
    data: dict = {}
    for n in food_nutrients:
        nutrient = n.get("nutrient", n)
        nid = nutrient.get("id") or nutrient.get("nutrientId")
        # Prefer "amount" (detail API) over "value" (search API); use explicit None
        # check so a genuine 0.0 amount is not skipped by Python's truthiness rules.
        raw = n.get("amount")
        if raw is None:
            raw = n.get("value")
        amount = raw if raw is not None else 0.0
        if nid in NUTRIENT_MAP:
            field = NUTRIENT_MAP[nid]
            val = round(float(amount), 4)
            if field == "calories":
                # Two IDs map to calories: 1008 (kcal) and 2048 (kcal, Atwater
                # Specific). ID 1062 is excluded from NUTRIENT_MAP because SR Legacy
                # stores it in kJ, causing ~4x inflation. Both remaining IDs are kcal
                # so taking the max is safe and order-independent.
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


def fetch_all_foods(data_type: str) -> list[tuple]:
    """Paginate through USDA API for a given data type. Returns list of row tuples."""
    rows: list[tuple] = []
    page = 1

    with httpx.Client(timeout=30.0) as client:
        # Get total count first
        resp = client.post(
            f"{USDA_BASE_URL}/foods/search",
            params={"api_key": USDA_API_KEY},
            json={"query": "", "dataType": [data_type], "pageSize": 1, "pageNumber": 1},
        )
        resp.raise_for_status()
        total = resp.json().get("totalHits", 0)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        print(f"  {data_type}: {total} foods, {total_pages} pages")

        while page <= total_pages:
            resp = client.post(
                f"{USDA_BASE_URL}/foods/search",
                params={"api_key": USDA_API_KEY},
                json={
                    "query": "",
                    "dataType": [data_type],
                    "pageSize": PAGE_SIZE,
                    "pageNumber": page,
                },
            )
            if resp.status_code == 429:
                print(f"\n  Rate limited — waiting 60s (page {page}/{total_pages})...")
                time.sleep(60)
                continue
            resp.raise_for_status()

            foods = resp.json().get("foods", [])
            for item in foods:
                fdc_id = item.get("fdcId")
                desc = item.get("description", "").strip()
                if not fdc_id or not desc:
                    continue
                raw_cat = item.get("foodCategory") or item.get("foodCategoryLabel")
                if isinstance(raw_cat, dict):
                    raw_cat = raw_cat.get("description")
                nutrients = _parse_nutrients(item.get("foodNutrients", []))
                rows.append((fdc_id, desc, raw_cat, data_type, json.dumps(nutrients)))

            print(f"  {data_type}: page {page}/{total_pages} ({len(rows)} so far)\r", end="", flush=True)
            page += 1

            # Brief pause to stay under DEMO_KEY rate limit (30 req/hr = 1 req/120s)
            if USDA_API_KEY == "DEMO_KEY":
                time.sleep(120)

    print(f"  {data_type}: {len(rows)} foods fetched          ")
    return rows


def upsert_batch(conn, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO foods (fdc_id, description, food_category, data_type, nutrients)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fdc_id) DO UPDATE
              SET description   = EXCLUDED.description,
                  food_category = EXCLUDED.food_category,
                  nutrients     = EXCLUDED.nutrients,
                  cached_at     = NOW()
            """,
            rows,
        )
    conn.commit()


def main() -> None:
    if USDA_API_KEY == "DEMO_KEY":
        print(
            "Using DEMO_KEY (30 req/hr). SR Legacy has ~40 pages so this will take ~70 min.\n"
            "Set USDA_API_KEY in your .env for a free registered key (1,000 req/hr → ~5 min).\n"
            "Sign up at https://fdc.nal.usda.gov/api-key-signup.html\n"
        )

    all_rows: list[tuple] = []
    for data_type in DATA_TYPES:
        try:
            rows = fetch_all_foods(data_type)
            all_rows.extend(rows)
        except Exception as e:
            print(f"\n  ERROR fetching {data_type}: {e}", file=sys.stderr)

    if not all_rows:
        print("No data fetched — aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"\nInserting {len(all_rows)} foods into Postgres...")
    db_url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(db_url) as conn:
        inserted = 0
        for i in range(0, len(all_rows), BATCH_SIZE):
            batch = all_rows[i : i + BATCH_SIZE]
            upsert_batch(conn, batch)
            inserted += len(batch)
            print(f"  {inserted}/{len(all_rows)} ({inserted/len(all_rows)*100:.0f}%)\r", end="", flush=True)

    print(f"\nDone. {inserted} foods upserted.")

    print("\nPost-ingest data quality check:")
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM foods WHERE (nutrients->>'calories')::numeric = 0")
            zero_cal = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM foods
                WHERE (nutrients->>'calories')::numeric = 0
                  AND ((nutrients->>'fat_g')::numeric     > 0
                    OR (nutrients->>'protein_g')::numeric > 0
                    OR (nutrients->>'carbs_g')::numeric   > 0)
                """
            )
            fixable = cur.fetchone()[0]
    print(f"  Zero-calorie records: {zero_cal}")
    if fixable:
        print(f"  WARNING: {fixable} records have 0 calories but non-zero macros.")
        print("  Run: uv run python scripts/repair_zero_calories.py")


if __name__ == "__main__":
    main()
