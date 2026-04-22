import AsyncStorage from "@react-native-async-storage/async-storage";

const USER_ID_KEY = "meal_planner_user_id";
const SESSION_ID_KEY = "meal_planner_session_id";
const PENDING_PROMPT_KEY = "meal_planner_pending_prompt";

export async function saveUserId(userId: string): Promise<void> {
  await AsyncStorage.setItem(USER_ID_KEY, userId);
}

export async function loadUserId(): Promise<string | null> {
  return AsyncStorage.getItem(USER_ID_KEY);
}

export async function clearUserId(): Promise<void> {
  await AsyncStorage.removeItem(USER_ID_KEY);
}

export async function saveSessionId(sessionId: string): Promise<void> {
  await AsyncStorage.setItem(SESSION_ID_KEY, sessionId);
}

export async function loadSessionId(): Promise<string | null> {
  return AsyncStorage.getItem(SESSION_ID_KEY);
}

export async function clearSessionId(): Promise<void> {
  await AsyncStorage.removeItem(SESSION_ID_KEY);
}

export async function savePendingPrompt(prompt: string): Promise<void> {
  await AsyncStorage.setItem(PENDING_PROMPT_KEY, prompt);
}

export async function loadPendingPrompt(): Promise<string | null> {
  return AsyncStorage.getItem(PENDING_PROMPT_KEY);
}

export async function clearPendingPrompt(): Promise<void> {
  await AsyncStorage.removeItem(PENDING_PROMPT_KEY);
}
