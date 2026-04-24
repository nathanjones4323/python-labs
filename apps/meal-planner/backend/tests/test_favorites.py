"""
Tests for favorites endpoints: add, list, remove, edge cases.

All DB calls are mocked — no real database required.
"""

import uuid
import json
import pytest
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient

from tests.conftest import (
    USER_ID, MEAL_ID, FOOD_FDC_CHICKEN, FOOD_FDC_RICE,
    CHICKEN_NUTRIENTS, RICE_NUTRIENTS, ZERO_NUTRIENTS,
    _make_meal_row, _make_food_rows, _make_fav_id,
)


# ---------------------------------------------------------------------------
# App client fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    from meal_planner.main import app
    return TestClient(app)


def _mock_fav_response(fav_id: str, source_meal_id: str | None = None):
    """Build the row tuple returned by _load_favorite_response."""
    return (
        fav_id, USER_ID, "Grilled Chicken Bowl", "dinner",
        ["Grill chicken 6 min per side", "Serve over rice"],
        None, source_meal_id, "2026-04-22T00:00:00",
    )


def _mock_fav_food_rows(fav_id: str):
    return [
        (str(uuid.uuid4()), FOOD_FDC_CHICKEN, "Chicken breast, grilled", None, CHICKEN_NUTRIENTS, 150.0, None),
        (str(uuid.uuid4()), FOOD_FDC_RICE,    "Rice, white, cooked",     None, RICE_NUTRIENTS,    200.0, None),
    ]


# ---------------------------------------------------------------------------
# Helper: build a mock connection that handles the favorite_meal queries
# ---------------------------------------------------------------------------
def _make_fav_conn(fav_id: str, source_meal_id: str | None = None):
    conn = MagicMock()

    def execute_side_effect(sql, params=None):
        sql_stripped = " ".join(sql.split())
        cursor = MagicMock()

        if "SELECT id, user_id, name" in sql_stripped:
            cursor.fetchone.return_value = _mock_fav_response(fav_id, source_meal_id)
        elif "SELECT fmf.id, fmf.fdc_id, f.description" in sql_stripped:
            cursor.fetchall.return_value = _mock_fav_food_rows(fav_id)
        elif "SELECT id FROM favorite_meals WHERE user_id" in sql_stripped:
            cursor.fetchall.return_value = [(fav_id,)]
        elif "DELETE FROM favorite_meals" in sql_stripped:
            cursor.fetchone.return_value = (fav_id,)
        elif "SELECT id, name, meal_type, instructions, notes FROM meals" in sql_stripped:
            cursor.fetchone.return_value = _make_meal_row()
        elif "SELECT fdc_id, quantity_g, serving_description FROM meal_foods" in sql_stripped:
            cursor.fetchall.return_value = [
                (FOOD_FDC_CHICKEN, 150.0, None),
                (FOOD_FDC_RICE,    200.0, None),
            ]
        elif "INSERT INTO favorite_meals" in sql_stripped:
            cursor.fetchone.return_value = None
        elif "INSERT INTO favorite_meal_foods" in sql_stripped:
            cursor.fetchone.return_value = None
        else:
            cursor.fetchone.return_value = None
            cursor.fetchall.return_value = []
        return cursor

    conn.execute.side_effect = execute_side_effect
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFavoriteAdd:
    def test_favorite_meal_success(self, client):
        fav_id = _make_fav_id()
        conn = _make_fav_conn(fav_id, source_meal_id=MEAL_ID)

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.post(f"/favorites?user_id={USER_ID}", json={"meal_id": MEAL_ID})

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Grilled Chicken Bowl"
        assert data["source_meal_id"] == MEAL_ID
        assert len(data["foods"]) == 2

    def test_favorite_meal_not_found(self, client):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.post(f"/favorites?user_id={USER_ID}", json={"meal_id": str(uuid.uuid4())})

        assert resp.status_code == 404

    def test_duplicate_favorite_creates_second_entry(self, client):
        """Favoriting the same meal twice creates two separate favorite entries."""
        fav_id_1 = _make_fav_id()
        fav_id_2 = _make_fav_id()
        conn1 = _make_fav_conn(fav_id_1, source_meal_id=MEAL_ID)
        conn2 = _make_fav_conn(fav_id_2, source_meal_id=MEAL_ID)

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn1)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            resp1 = client.post(f"/favorites?user_id={USER_ID}", json={"meal_id": MEAL_ID})

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn2)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            resp2 = client.post(f"/favorites?user_id={USER_ID}", json={"meal_id": MEAL_ID})

        assert resp1.status_code == 201
        assert resp2.status_code == 201


class TestFavoriteList:
    def test_list_favorites_returns_all(self, client):
        fav_id = _make_fav_id()
        conn = _make_fav_conn(fav_id, source_meal_id=MEAL_ID)

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.get(f"/favorites?user_id={USER_ID}")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Grilled Chicken Bowl"

    def test_list_favorites_empty_for_new_user(self, client):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.get(f"/favorites?user_id={USER_ID}")

        assert resp.status_code == 200
        assert resp.json() == []


class TestFavoriteRemove:
    def test_unfavorite_success(self, client):
        fav_id = _make_fav_id()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (fav_id,)

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.delete(f"/favorites/{fav_id}")

        assert resp.status_code == 204

    def test_unfavorite_not_found(self, client):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.delete(f"/favorites/{uuid.uuid4()}")

        assert resp.status_code == 404

    def test_unfavorite_then_list_is_empty(self, client):
        """After deletion, listing returns no results."""
        fav_id = _make_fav_id()

        delete_conn = MagicMock()
        delete_conn.execute.return_value.fetchone.return_value = (fav_id,)

        list_conn = MagicMock()
        list_conn.execute.return_value.fetchall.return_value = []

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=delete_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            client.delete(f"/favorites/{fav_id}")

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=list_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
            resp = client.get(f"/favorites?user_id={USER_ID}")

        assert resp.status_code == 200
        assert resp.json() == []


class TestFavoritePersistence:
    def test_get_single_favorite(self, client):
        fav_id = _make_fav_id()
        conn = _make_fav_conn(fav_id, source_meal_id=MEAL_ID)

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.get(f"/favorites/{fav_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fav_id
        assert data["user_id"] == USER_ID
        assert len(data["foods"]) == 2
        assert any(f["fdc_id"] == FOOD_FDC_CHICKEN for f in data["foods"])

    def test_favoriting_deleted_meal_preserves_data(self, client):
        """Favorited meal data is stored independently — remains accessible even after
        source_meal_id is no longer in the meals table."""
        fav_id = _make_fav_id()
        conn = _make_fav_conn(fav_id, source_meal_id=None)  # source_meal_id = NULL (deleted)

        with patch("meal_planner.routers.favorites.get_conn") as mock_get_conn:
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.get(f"/favorites/{fav_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_meal_id"] is None
        assert data["name"] == "Grilled Chicken Bowl"
        assert len(data["foods"]) == 2
