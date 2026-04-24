# Changes — meal-planner-edit-fixes

## Bug 1 — Inline AI Edit on Meal Cards (highest priority)

**Problem:** "Ask AI to swap" on plan cards navigated away to the Chat tab and injected a pre-populated prompt. No context about the specific meal was maintained. Additionally, even on the meal detail page, the existing `ai-edit` endpoint wrote changes to the database *before* the user confirmed, making the "Discard" button functionally broken.

**Fix:**
- **Backend:** Added `POST /meals/{meal_id}/ai-preview` — runs the Claude agent and computes full nutrition for proposed foods, but makes **no DB writes**. Returns a `MealResponse` identical in shape to the normal endpoint.
- **Frontend (`plans.tsx`):** Replaced the "Ask AI to swap" chip with an "Edit with AI" button that opens an inline bottom-sheet modal. The modal has two phases: (1) instruction input, (2) diff view showing nutrition deltas + per-ingredient add/remove/change status. User can accept (calls `PUT /meals/{id}`) or go back.
- **Frontend (`meal/[id].tsx`):** Changed `handleAIEdit` to call `aiPreviewMeal` (no DB write). `applyAIResult` now calls `updateMeal` to persist on confirm. Discard truly discards — the DB is never touched until the user clicks "Apply Changes".

## Bug 2 — Unauthenticated Users Spin Forever on Protected Routes

**Problem:** Navigating directly to `/plans` or any tab route without a stored user ID left the `loading` state permanently `true` since `loadSummaries()` was never called (it requires `userId`), so the spinner never resolved.

**Fix:** Added an auth guard to `app/(tabs)/_layout.tsx`. On mount it reads from AsyncStorage; if no user ID is found it calls `router.replace("/auth")` immediately and shows a spinner until the check completes. All tabs are protected by this single guard — no per-screen duplication needed.

## Feature — Dev Auth Bypass

**Problem:** Developers had to go through Google OAuth on every fresh local run.

**Fix:**
- **Backend:** Added `POST /auth/dev-bypass` endpoint, only active when `DEV_MODE=true` in the environment. Creates (or reuses) a `dev@localhost` user and returns a user ID — no OAuth needed.
- **Frontend (`auth.tsx`):** When `EXPO_PUBLIC_DEV_BYPASS_AUTH=true`, a clearly-labelled "Dev Bypass Auth" button appears in a dashed orange box below the normal sign-in UI. The button is **completely invisible** when the env var is absent or not `"true"` — zero production impact.
- **`.env.example`:** Add `DEV_MODE=false` and `EXPO_PUBLIC_DEV_BYPASS_AUTH=false` to template.

## Tests

Added `apps/meal-planner/backend/tests/`:

| File | Coverage |
|---|---|
| `test_favorites.py` | Add, list, remove, duplicate favorite, favorite then delete meal (source_meal_id=NULL), empty list for new user |
| `test_ai_edit.py` | Preview returns proposed meal without DB write, nutrition computed correctly, 422 when AI fails, diff correctness (removed/added foods), accept persists via PUT, reject leaves DB unchanged |

All 17 tests pass. The Anthropic API is mocked — no real API calls during test runs.

## Files Changed

### Backend
- `apps/meal-planner/backend/src/meal_planner/config.py` — added `DEV_MODE`
- `apps/meal-planner/backend/src/meal_planner/routers/auth.py` — added `/auth/dev-bypass`
- `apps/meal-planner/backend/src/meal_planner/routers/meals.py` — added `/meals/{id}/ai-preview`
- `apps/meal-planner/backend/pyproject.toml` — added pytest dev dependency group
- `apps/meal-planner/backend/tests/` — new test suite

### Frontend
- `apps/meal-planner/frontend/lib/api.ts` — added `aiPreviewMeal`, `devBypassAuth`
- `apps/meal-planner/frontend/app/(tabs)/_layout.tsx` — auth guard
- `apps/meal-planner/frontend/app/(tabs)/plans.tsx` — inline AI edit modal
- `apps/meal-planner/frontend/app/meal/[id].tsx` — fixed preview-then-confirm flow
- `apps/meal-planner/frontend/app/auth.tsx` — dev bypass button
