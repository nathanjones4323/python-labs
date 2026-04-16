import "../global.css";
import { useEffect, useState } from "react";
import { Stack, useRouter } from "expo-router";
import { loadUserId } from "../lib/storage";
import { getUser } from "../lib/api";

export default function RootLayout() {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const userId = await loadUserId();
        if (userId) {
          const user = await getUser(userId);
          if (user.profile?.onboarding_complete) {
            router.replace("/(tabs)/chat");
          } else {
            router.replace("/onboarding");
          }
        } else {
          router.replace("/onboarding");
        }
      } catch {
        router.replace("/onboarding");
      } finally {
        setReady(true);
      }
    })();
  }, []);

  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Screen name="onboarding" />
      <Stack.Screen name="(tabs)" />
      <Stack.Screen name="meal/[id]" options={{ headerShown: true, title: "Meal Detail" }} />
    </Stack>
  );
}
