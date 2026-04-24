import Constants from "expo-constants";
import {
  ChatSession,
  FavoriteMeal,
  FavoriteMealSummary,
  FoodSearchResult,
  Meal,
  MealPlan,
  MealPlanSummary,
  NutrientSummary,
  User,
  UserProfile,
} from "./types";

const BASE_URL =
  (Constants.expoConfig?.extra?.apiUrl as string | undefined) ||
  process.env.EXPO_PUBLIC_API_URL ||
  "http://localhost:8000";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API error ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ─── Auth ─────────────────────────────────────────────────────────────────────

export async function signInWithGoogle(accessToken: string): Promise<{
  user_id: string;
  email: string;
  display_name: string | null;
  is_new_user: boolean;
  profile: UserProfile | null;
}> {
  return request("/auth/google", {
    method: "POST",
    body: JSON.stringify({ access_token: accessToken }),
  });
}

export async function devBypassAuth(): Promise<{
  user_id: string;
  email: string;
  display_name: string | null;
  is_new_user: boolean;
  profile: UserProfile | null;
}> {
  return request("/auth/dev-bypass", { method: "POST" });
}

// ─── Users ────────────────────────────────────────────────────────────────────

export async function createUser(email: string): Promise<User> {
  return request<User>("/users", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function getUser(userId: string): Promise<User> {
  return request<User>(`/users/${userId}`);
}

export async function upsertProfile(
  userId: string,
  profile: Omit<UserProfile, "user_id" | "onboarding_complete">
): Promise<UserProfile> {
  return request<UserProfile>(`/users/${userId}/profile`, {
    method: "PUT",
    body: JSON.stringify(profile),
  });
}

export async function patchProfile(
  userId: string,
  patch: Partial<Omit<UserProfile, "user_id" | "onboarding_complete">>
): Promise<UserProfile> {
  return request<UserProfile>(`/users/${userId}/profile`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

// ─── Meal Plans ───────────────────────────────────────────────────────────────

export async function listMealPlans(userId: string): Promise<MealPlanSummary[]> {
  return request<MealPlanSummary[]>(`/meal-plans?user_id=${userId}`);
}

export async function getMealPlan(planId: string): Promise<MealPlan> {
  return request<MealPlan>(`/meal-plans/${planId}`);
}

export async function deleteMealPlan(planId: string): Promise<void> {
  await request<void>(`/meal-plans/${planId}`, { method: "DELETE" });
}

// ─── Meals ────────────────────────────────────────────────────────────────────

export async function updateMeal(
  mealId: string,
  body: {
    name?: string;
    meal_type?: string;
    instructions?: string[];
    notes?: string;
    foods?: Array<{ fdc_id: number; quantity_g: number; serving_description?: string }>;
  }
): Promise<Meal> {
  return request<Meal>(`/meals/${mealId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function aiPreviewMeal(
  mealId: string,
  instruction: string,
  userId: string
): Promise<Meal> {
  return request<Meal>(`/meals/${mealId}/ai-preview`, {
    method: "POST",
    body: JSON.stringify({ instruction, user_id: userId }),
  });
}

export async function aiEditMeal(
  mealId: string,
  instruction: string,
  userId: string
): Promise<Meal> {
  return request<Meal>(`/meals/${mealId}/ai-edit`, {
    method: "POST",
    body: JSON.stringify({ instruction, user_id: userId }),
  });
}

// ─── Foods ────────────────────────────────────────────────────────────────────

export async function searchFoods(
  q: string,
  limit = 20
): Promise<{ results: FoodSearchResult[]; total: number }> {
  return request(`/foods/search?q=${encodeURIComponent(q)}&limit=${limit}`);
}

// ─── Favorites ────────────────────────────────────────────────────────────────

export async function listFavorites(userId: string): Promise<FavoriteMeal[]> {
  return request<FavoriteMeal[]>(`/favorites?user_id=${userId}`);
}

export async function listFavoriteSummaries(userId: string): Promise<FavoriteMealSummary[]> {
  return request<FavoriteMealSummary[]>(`/favorites/summaries?user_id=${userId}`);
}

export async function favoriteMeal(userId: string, mealId: string): Promise<FavoriteMeal> {
  return request<FavoriteMeal>(`/favorites?user_id=${userId}`, {
    method: "POST",
    body: JSON.stringify({ meal_id: mealId }),
  });
}

export async function getFavorite(favoriteId: string): Promise<FavoriteMeal> {
  return request<FavoriteMeal>(`/favorites/${favoriteId}`);
}

export async function unfavoriteMeal(favoriteId: string): Promise<void> {
  await request<void>(`/favorites/${favoriteId}`, { method: "DELETE" });
}

// ─── Chat ──────────────────────────────────────────────────────────────────────

interface ChatEvent {
  type: "text" | "tool_use" | "plan_created" | "plan_saved" | "session_created" | "error" | "done";
  content?: string;
  name?: string;
  label?: string;
  plan?: unknown;
  plan_id?: string;
  session_id?: string;
  message?: string;
}

export async function* chatStream(
  userId: string,
  message: string,
  sessionId?: string | null,
  favoriteIds?: string[]
): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      message,
      session_id: sessionId ?? null,
      favorite_ids: favoriteIds ?? [],
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`Chat error ${res.status}: ${text}`);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (data === "[DONE]") {
        yield { type: "done" };
        return;
      }
      try {
        const event = JSON.parse(data) as ChatEvent;
        yield event;
      } catch {
        // skip malformed lines
      }
    }
  }
}

export async function endChatSession(userId: string, sessionId: string): Promise<void> {
  await request<void>(`/chat/${userId}/sessions/${sessionId}/end`, { method: "POST" });
}

export async function listChatSessions(userId: string): Promise<ChatSession[]> {
  return request<ChatSession[]>(`/chat/${userId}/sessions`);
}

export async function getChatSession(
  userId: string,
  sessionId: string
): Promise<{ id: string; title: string; messages: Array<{ role: string; content: string }>; updated_at: string | null }> {
  return request(`/chat/${userId}/sessions/${sessionId}`);
}

export async function deleteChatSession(userId: string, sessionId: string): Promise<void> {
  await request<void>(`/chat/${userId}/sessions/${sessionId}`, { method: "DELETE" });
}

export async function clearChatHistory(userId: string): Promise<void> {
  await request<void>(`/chat/${userId}/history`, { method: "DELETE" });
}
