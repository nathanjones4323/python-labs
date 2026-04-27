import "../global.css";
import { Stack } from "expo-router";

export default function RootLayout() {
  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Screen name="index" />
      <Stack.Screen name="auth" />
      <Stack.Screen name="onboarding" />
      <Stack.Screen name="(tabs)" />
      <Stack.Screen name="meal/[id]" options={{ headerShown: true, title: "Meal Detail" }} />
      <Stack.Screen name="create-recipe" options={{ headerShown: true, title: "New Recipe" }} />
    </Stack>
  );
}
