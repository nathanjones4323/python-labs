#!/usr/bin/env python3
"""
One-shot USDA FoodData Central ingestion script.

Two-phase approach:
  Phase 1 — search API (paginated) to collect all fdc_ids for Foundation and
             SR Legacy foods. Fast: ~42 requests total.
  Phase 2 — bulk detail API (/foods POST, format=full) in batches of 20 to
             fetch complete nutrient profiles. This is what fills vitamin D,
             fiber, sugar, and all micronutrients that the search API omits.

Safe to re-run — uses ON CONFLICT DO UPDATE.

Usage:
    uv run python scripts/ingest_usda.py

With a registered USDA_API_KEY (1,000 req/hr): ~30 minutes total.
With DEMO_KEY (30 req/hr): phase 2 alone takes ~14 hours — get a free key.
Sign up at https://fdc.nal.usda.gov/api-key-signup.html

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

PAGE_SIZE = 200  # max allowed by USDA search API
DETAIL_BATCH = 20  # max fdc_ids per /foods bulk request
DB_BATCH = 200  # rows per Postgres executemany
DATA_TYPES = ["Foundation", "SR Legacy"]
# Foundation alone is only ~400 foods; a real seed lands in the thousands, so
# anything under this means a previous run died partway through.
SEEDED_THRESHOLD = 5000
# FDC's HTML-404 flakiness can persist for a stretch, and losing one data type
# to it leaves a silently half-seeded database — so retry patiently.
RETRY_ATTEMPTS = 10


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
    if not data.get("calories") and (
        data.get("protein_g") or data.get("carbs_g") or data.get("fat_g")
    ):
        data["calories"] = round(
            data.get("protein_g", 0) * 4
            + data.get("carbs_g", 0) * 4
            + data.get("fat_g", 0) * 9,
            4,
        )
    return data


def _sleep_if_demo(secs: float = 120.0) -> None:
    if USDA_API_KEY == "DEMO_KEY":
        time.sleep(secs)


def _post_with_retry(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """POST to FDC, retrying its intermittent HTML 404s and 5xx.

    FDC sporadically serves its HTML SPA 404 page for valid requests (~30% of
    calls in testing). Without this a single bad roll aborts the whole ingest.
    A genuine JSON 404 is returned to the caller untouched.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        resp = client.post(url, **kwargs)
        is_html_404 = resp.status_code == 404 and not resp.headers.get(
            "content-type", ""
        ).startswith("application/json")
        if not (is_html_404 or resp.status_code in (500, 502, 503, 504)):
            return resp
        wait = min(0.5 * 2 ** (attempt - 1), 30.0)
        print(
            f"\n  transient {resp.status_code} from FDC — "
            f"retry {attempt}/{RETRY_ATTEMPTS} in {wait:.1f}s",
            file=sys.stderr,
        )
        time.sleep(wait)
    return resp


def collect_search_data(
    data_type: str, client: httpx.Client
) -> tuple[list[int], dict[int, tuple]]:
    """Phase 1: paginate search to collect fdc_ids and search-derived row tuples.

    Returns (ordered_ids, fallback) where fallback maps fdc_id → row tuple built
    from search results. The fallback is used in Phase 2 for any ID the detail
    API silently drops — a known USDA behaviour for certain Foundation foods.
    """
    ids: list[int] = []
    fallback: dict[int, tuple] = {}
    page = 1

    resp = _post_with_retry(
        client,
        f"{USDA_BASE_URL}/foods/search",
        params={"api_key": USDA_API_KEY},
        json={"query": "", "dataType": [data_type], "pageSize": 1, "pageNumber": 1},
    )
    resp.raise_for_status()
    total = resp.json().get("totalHits", 0)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  {data_type}: {total} foods, {total_pages} pages")
    _sleep_if_demo()

    while page <= total_pages:
        resp = _post_with_retry(
            client,
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
        for item in resp.json().get("foods", []):
            fdc_id = item.get("fdcId")
            if not fdc_id:
                continue
            ids.append(fdc_id)
            if row := parse_row(item):
                fallback[fdc_id] = row
        print(
            f"  {data_type}: page {page}/{total_pages} ({len(ids)} ids)\r",
            end="",
            flush=True,
        )
        page += 1
        _sleep_if_demo()

    print(f"  {data_type}: {len(ids)} food IDs collected          ")
    return ids, fallback


def fetch_detail_batch(fdc_ids: list[int], client: httpx.Client) -> list[dict]:
    """Phase 2: fetch full nutrient profiles for up to DETAIL_BATCH fdc_ids."""
    while True:
        resp = _post_with_retry(
            client,
            f"{USDA_BASE_URL}/foods",
            params={"api_key": USDA_API_KEY, "format": "full"},
            json={"fdcIds": fdc_ids},
        )
        if resp.status_code == 429:
            print("\n  Rate limited — waiting 60s...")
            time.sleep(60)
            continue
        resp.raise_for_status()
        return resp.json()


def parse_row(item: dict) -> tuple | None:
    fdc_id = item.get("fdcId")
    desc = (item.get("description") or "").strip()
    if not fdc_id or not desc:
        return None
    raw_cat = item.get("foodCategory") or item.get("foodCategoryLabel")
    if isinstance(raw_cat, dict):
        raw_cat = raw_cat.get("description")
    data_type = item.get("dataType")
    nutrients = _parse_nutrients(item.get("foodNutrients", []))
    return (fdc_id, desc, raw_cat, data_type, json.dumps(nutrients))


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


def _food_count(db_url: str) -> int:
    with psycopg.connect(db_url) as conn:
        return conn.execute("SELECT count(*) FROM foods").fetchone()[0]


def main() -> None:
    db_url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)

    # `make seed` passes this so re-running against a seeded database is a
    # no-op instead of another 30-minute pull.
    if "--skip-if-populated" in sys.argv:
        existing = _food_count(db_url)
        if existing >= SEEDED_THRESHOLD:
            print(f"foods table already has {existing} rows — nothing to do.")
            return
        print(f"foods table has {existing} rows — seeding.")

    if USDA_API_KEY == "DEMO_KEY":
        print(
            "Using DEMO_KEY (30 req/hr). Phase 2 (detail fetch) will take ~14 hours.\n"
            "Get a free registered key for 1,000 req/hr (~30 min total).\n"
            "Sign up at https://fdc.nal.usda.gov/api-key-signup.html\n"
        )

    # Phase 1 — collect all fdc_ids and search-derived fallback rows
    print("Phase 1: collecting food IDs via search API...")
    all_ids: list[int] = []
    search_fallback: dict[int, tuple] = {}
    failed_types: list[str] = []
    with httpx.Client(timeout=120.0) as client:
        for data_type in DATA_TYPES:
            try:
                ids, fallback = collect_search_data(data_type, client)
                all_ids.extend(ids)
                search_fallback.update(fallback)
            except Exception as e:
                print(f"\n  ERROR collecting IDs for {data_type}: {e}", file=sys.stderr)
                failed_types.append(data_type)

    if failed_types:
        # Continuing would upsert a partial catalog that looks successful and
        # then quietly fails to match ingredients at recipe time.
        print(
            f"\nCould not collect: {', '.join(failed_types)}. "
            "Refusing to write a partial catalog — re-run once FDC settles.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not all_ids:
        print("No food IDs collected — aborting.", file=sys.stderr)
        sys.exit(1)

    print(
        f"\nPhase 2: fetching full nutrient detail for {len(all_ids)} foods "
        f"({len(all_ids) // DETAIL_BATCH + 1} batches of {DETAIL_BATCH})..."
    )

    # Phase 2 — fetch full detail in batches, parse, upsert as we go.
    # If the detail API silently drops IDs from a batch (a known USDA behaviour
    # for certain Foundation foods that cause server-side timeouts), fall back to
    # the search-derived row captured in Phase 1.
    processed = 0
    errors = 0
    fallback_used = 0
    total_batches = (len(all_ids) + DETAIL_BATCH - 1) // DETAIL_BATCH

    with httpx.Client(timeout=120.0) as client, psycopg.connect(db_url) as conn:
        for batch_num, i in enumerate(range(0, len(all_ids), DETAIL_BATCH), 1):
            batch_ids = all_ids[i : i + DETAIL_BATCH]
            try:
                items = fetch_detail_batch(batch_ids, client)
            except Exception as e:
                # A whole-batch failure used to drop all 20 foods outright, even
                # though Phase 1 already captured a usable row for each of them.
                # Write those instead of losing the foods entirely — that is how
                # staples like "Limes, raw" went missing from a seeded database.
                print(f"\n  ERROR on batch {batch_num}: {e}", file=sys.stderr)
                salvaged = [
                    search_fallback[fdc_id]
                    for fdc_id in batch_ids
                    if fdc_id in search_fallback
                ]
                if salvaged:
                    upsert_batch(conn, salvaged)
                    fallback_used += len(salvaged)
                    print(
                        f"  recovered {len(salvaged)}/{len(batch_ids)} from search data",
                        file=sys.stderr,
                    )
                errors += len(batch_ids) - len(salvaged)
                processed += len(batch_ids)
                _sleep_if_demo()
                continue

            returned_ids = {item["fdcId"] for item in items}
            rows = [r for item in items if (r := parse_row(item))]

            # Fall back to search data for any ID the detail API dropped
            for fdc_id in batch_ids:
                if fdc_id not in returned_ids and fdc_id in search_fallback:
                    rows.append(search_fallback[fdc_id])
                    fallback_used += 1

            if rows:
                upsert_batch(conn, rows)
            processed += len(batch_ids)

            pct = processed / len(all_ids) * 100
            print(
                f"  batch {batch_num}/{total_batches} — {processed}/{len(all_ids)} ({pct:.0f}%)\r",
                end="",
                flush=True,
            )
            _sleep_if_demo()

    summary = f"{processed - errors} foods upserted ({errors} errors)"
    if fallback_used:
        summary += f", {fallback_used} used search-API fallback (detail API dropped them)"
    print(f"\nDone. {summary}.")

    print("\nPost-ingest data quality check:")
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM foods WHERE (nutrients->>'calories')::numeric = 0"
            )
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
