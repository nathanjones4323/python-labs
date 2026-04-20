import { Redirect } from "expo-router";
import { useEffect, useState } from "react";
import { View, ActivityIndicator } from "react-native";
import { loadUserId } from "../lib/storage";
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
      } catch {
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
