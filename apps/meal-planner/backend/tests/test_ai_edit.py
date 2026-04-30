"""
Tests for the inline AI edit flow:
  - /meals/{id}/ai-preview returns proposed meal without saving
  - diff is computed correctly (added / removed / changed foods)
  - /meals/{id} PUT applies confirmed changes
  - preview revert: reject means DB is untouched

Uses claude-haiku-4-5-20251001 for any real Anthropic call (mocked by default).
"""

import uuid
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient

from tests.conftest import (
    USER_ID, MEAL_ID, FOOD_FDC_CHICKEN, FOOD_FDC_SALMON, FOOD_FDC_RICE,
    CHICKEN_NUTRIENTS, SALMON_NUTRIENTS, RICE_NUTRIENTS, ZERO_NUTRIENTS,
    _make_meal_row, _make_food_rows, _make_fav_id,
)


# ---------------------------------------------------------------------------
# App client fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    from meal_planner.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Mocked Anthropic response: "replace chicken with salmon"
# ---------------------------------------------------------------------------

def _mock_update_meal_tool_block():
    """Simulate Claude calling update_meal after replacing chicken with salmon."""
    import anthropic
    tool_block = MagicMock(spec=anthropic.types.ToolUseBlock)
    tool_block.name = "update_meal"
    tool_block.id = "toolu_mock_01"
    tool_block.input = {
        "name": "Grilled Salmon Bowl",
        "instructions": ["Season salmon with salt", "Cook salmon 4 min per side", "Serve over rice"],
        "foods": [
            {"fdc_id": FOOD_FDC_SALMON, "quantity_g": 150.0, "serving_description": None},
            {"fdc_id": FOOD_FDC_RICE,   "quantity_g": 200.0, "serving_description": None},
        ],
    }
    return tool_block


def _mock_anthropic_response(stop_reason="tool_use"):
    import anthropic
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = [_mock_update_meal_tool_block()]
    return resp


# ---------------------------------------------------------------------------
# Helpers: mock connections
# ---------------------------------------------------------------------------

def _make_meal_db_conn():
    """Connection that returns the existing chicken bowl meal."""
    conn = MagicMock()

    def execute_side_effect(sql, params=None):
        sql_stripped = " ".join(sql.split())
        cursor = MagicMock()

        if "SELECT id, external_id, name, meal_type" in sql_stripped:
            cursor.fetchone.return_value = _make_meal_row()
        elif "SELECT mf.id, mf.external_id, mf.fdc_id" in sql_stripped:
            cursor.fetchall.return_value = _make_food_rows()
        elif "SELECT description, food_category, nutrients FROM foods WHERE fdc_id" in sql_stripped:
            fdc_id = params[0] if params else None
            if fdc_id == FOOD_FDC_SALMON:
                cursor.fetchone.return_value = ("Atlantic salmon, raw", "Finfish", SALMON_NUTRIENTS)
            elif fdc_id == FOOD_FDC_RICE:
                cursor.fetchone.return_value = ("Rice, white, cooked", "Cereal Grains", RICE_NUTRIENTS)
            else:
                cursor.fetchone.return_value = None
        else:
            cursor.fetchone.return_value = None
            cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = execute_side_effect
    return conn


def _make_update_conn():
    """Connection for PUT /meals/{id} that updates and returns the saved meal."""
    conn = MagicMock()
    calls = []

    def execute_side_effect(sql, params=None):
        sql_stripped = " ".join(sql.split())
        cursor = MagicMock()
        calls.append(sql_stripped)

        if "SELECT id FROM meals WHERE id" in sql_stripped:
            cursor.fetchone.return_value = (MEAL_ID,)
        elif "UPDATE meals SET" in sql_stripped:
            cursor.fetchone.return_value = None
        elif "DELETE FROM meal_foods" in sql_stripped:
            cursor.fetchone.return_value = None
        elif "SELECT description, food_category, nutrients FROM foods WHERE fdc_id" in sql_stripped:
            fdc_id = params[0] if params else None
            if fdc_id == FOOD_FDC_SALMON:
                cursor.fetchone.return_value = ("Atlantic salmon, raw", "Finfish", SALMON_NUTRIENTS)
            elif fdc_id == FOOD_FDC_RICE:
                cursor.fetchone.return_value = ("Rice, white, cooked", "Cereal Grains", RICE_NUTRIENTS)
            else:
                cursor.fetchone.return_value = None
        elif "INSERT INTO meal_foods" in sql_stripped:
            cursor.fetchone.return_value = None
        elif "INSERT INTO foods" in sql_stripped:
            cursor.fetchone.return_value = None
        elif "SELECT id, external_id, name, meal_type" in sql_stripped:
            cursor.fetchone.return_value = (
                MEAL_ID, "meal_testexternal001", "Grilled Salmon Bowl", "dinner", "2026-04-22",
                ["Season salmon with salt", "Cook salmon 4 min per side", "Serve over rice"],
                None,
            )
        elif "SELECT mf.id, mf.external_id, mf.fdc_id" in sql_stripped:
            cursor.fetchall.return_value = [
                (str(uuid.uuid4()), "mfood_testexternal01", FOOD_FDC_SALMON, "Atlantic salmon, raw", SALMON_NUTRIENTS, 150.0, None),
                (str(uuid.uuid4()), "mfood_testexternal02", FOOD_FDC_RICE,   "Rice, white, cooked",  RICE_NUTRIENTS,  200.0, None),
            ]
        else:
            cursor.fetchone.return_value = None
            cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = execute_side_effect
    conn._calls = calls
    return conn


# ---------------------------------------------------------------------------
# Tests: ai-preview endpoint
# ---------------------------------------------------------------------------

class TestAIPreview:
    def test_preview_returns_proposed_meal_without_db_write(self, client):
        """
        POST /meals/{id}/ai-preview should return proposed MealResponse.
        The DB should NOT have any INSERT INTO meal_foods.
        """
        conn = _make_meal_db_conn()

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.services.meal_edit.client") as mock_client:

            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.messages.create = AsyncMock(return_value=_mock_anthropic_response())

            resp = client.post(
                f"/meals/{MEAL_ID}/ai-preview",
                json={"instruction": "replace chicken with salmon", "user_id": USER_ID},
            )

        assert resp.status_code == 200
        data = resp.json()

        # Name should be updated
        assert data["name"] == "Grilled Salmon Bowl"
        # Salmon is now in the foods
        fdc_ids = {f["fdc_id"] for f in data["foods"]}
        assert FOOD_FDC_SALMON in fdc_ids
        # Chicken is gone
        assert FOOD_FDC_CHICKEN not in fdc_ids
        # Rice is still present
        assert FOOD_FDC_RICE in fdc_ids

        # DB should NOT have inserted into meal_foods during preview
        insert_calls = [
            c for c in conn.execute.call_args_list
            if "INSERT INTO meal_foods" in " ".join(str(c).split())
        ]
        assert len(insert_calls) == 0, "Preview must not write to meal_foods table"

    def test_preview_nutrition_computed_correctly(self, client):
        """Total nutrition in preview = sum of scaled food nutrients."""
        conn = _make_meal_db_conn()

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.services.meal_edit.client") as mock_client:

            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.messages.create = AsyncMock(return_value=_mock_anthropic_response())

            resp = client.post(
                f"/meals/{MEAL_ID}/ai-preview",
                json={"instruction": "replace chicken with salmon", "user_id": USER_ID},
            )

        assert resp.status_code == 200
        data = resp.json()

        # Salmon 150g: 208*1.5=312 kcal; Rice 200g: 130*2=260 kcal → ~572 total
        expected_cal = (SALMON_NUTRIENTS["calories"] * 150 / 100) + (RICE_NUTRIENTS["calories"] * 200 / 100)
        actual_cal = data["total_nutrition"]["calories"]
        assert abs(actual_cal - expected_cal) < 1.0

    def test_preview_returns_422_when_ai_fails(self, client):
        """If the AI doesn't call update_meal, preview returns 422."""
        conn = _make_meal_db_conn()

        # Claude returns end_turn without calling any tool
        import anthropic
        empty_resp = MagicMock()
        empty_resp.stop_reason = "end_turn"
        empty_resp.content = []

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.services.meal_edit.client") as mock_client:

            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.messages.create = AsyncMock(return_value=empty_resp)

            resp = client.post(
                f"/meals/{MEAL_ID}/ai-preview",
                json={"instruction": "replace chicken with salmon", "user_id": USER_ID},
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: diff correctness (computed in frontend, but we validate preview data)
# ---------------------------------------------------------------------------

class TestDiffComputation:
    def test_removed_food_absent_from_preview(self, client):
        conn = _make_meal_db_conn()

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.services.meal_edit.client") as mock_client:

            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.messages.create = AsyncMock(return_value=_mock_anthropic_response())

            resp = client.post(
                f"/meals/{MEAL_ID}/ai-preview",
                json={"instruction": "replace chicken with salmon", "user_id": USER_ID},
            )

        data = resp.json()
        fdc_ids = {f["fdc_id"] for f in data["foods"]}
        assert FOOD_FDC_CHICKEN not in fdc_ids, "Chicken should be removed from preview"

    def test_added_food_present_in_preview(self, client):
        conn = _make_meal_db_conn()

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.services.meal_edit.client") as mock_client:

            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.messages.create = AsyncMock(return_value=_mock_anthropic_response())

            resp = client.post(
                f"/meals/{MEAL_ID}/ai-preview",
                json={"instruction": "replace chicken with salmon", "user_id": USER_ID},
            )

        data = resp.json()
        fdc_ids = {f["fdc_id"] for f in data["foods"]}
        assert FOOD_FDC_SALMON in fdc_ids, "Salmon should appear in preview as new food"


# ---------------------------------------------------------------------------
# Tests: accept (PUT /meals/{id}) vs reject (no DB write)
# ---------------------------------------------------------------------------

class TestAcceptReject:
    def test_apply_changes_persists_via_put(self, client):
        """Confirming the edit calls PUT /meals/{id}, which writes to DB."""
        update_conn = _make_update_conn()

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.routers.meal_plans._get_or_cache_food") as mock_cache:

            mock_cache.return_value = AsyncMock(return_value={
                "description": "Atlantic salmon, raw",
                "food_category": "Finfish",
                "nutrients": MagicMock(**SALMON_NUTRIENTS),
            })
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=update_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.put(
                f"/meals/{MEAL_ID}",
                json={
                    "name": "Grilled Salmon Bowl",
                    "instructions": ["Season salmon with salt", "Cook salmon 4 min per side"],
                    "foods": [
                        {"fdc_id": FOOD_FDC_SALMON, "quantity_g": 150.0},
                        {"fdc_id": FOOD_FDC_RICE,   "quantity_g": 200.0},
                    ],
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Grilled Salmon Bowl"

    def test_reject_leaves_db_unchanged(self, client):
        """Rejecting (discarding) the preview makes zero DB calls beyond the preview itself."""
        conn = _make_meal_db_conn()

        with patch("meal_planner.routers.meals.get_conn") as mock_get_conn, \
             patch("meal_planner.services.meal_edit.client") as mock_client:

            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.messages.create = AsyncMock(return_value=_mock_anthropic_response())

            # Preview — user calls this and then clicks Discard
            client.post(
                f"/meals/{MEAL_ID}/ai-preview",
                json={"instruction": "replace chicken with salmon", "user_id": USER_ID},
            )
            # No follow-up PUT is made (user rejected)

        # Verify: no INSERT or UPDATE happened on the meals table
        write_calls = [
            c for c in conn.execute.call_args_list
            if any(
                kw in " ".join(str(c).split())
                for kw in ("INSERT INTO meal_foods", "UPDATE meals SET", "DELETE FROM meal_foods")
            )
        ]
        assert len(write_calls) == 0, "Discarding preview must not modify the DB"
