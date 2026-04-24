import { Redirect } from "expo-router";
import { useEffect, useState } from "react";
import { View, ActivityIndicator } from "react-native";
import { loadUserId, clearUserId } from "../lib/storage";
import { getUser } from "../lib/api";

export default function Index() {
  const [destination, setDestination] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const userId = await loadUserId();
        if (userId) {
          const user = await getUser(userId);
          setDestination(user.profile?.onboarding_complete ? "/(tabs)/chat" : "/onboarding");
        } else {
          setDestination("/auth");
        }
      } catch (e: unknown) {
        // 404 = stored user ID no longer exists in the DB (DB was reset).
        // Clear the stale ID so the user can sign in fresh instead of looping.
        const is404 = e instanceof Error && e.message.includes("404");
        if (is404) await clearUserId();
        setDestination("/auth");
      }
    })();
  }, []);

  if (!destination) {
    return (
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: "white" }}>
        <ActivityIndicator color="#16a34a" />
      </View>
    );
  }

  return <Redirect href={destination as never} />;
}
