import { Redirect } from "expo-router";

// Root redirects to chat; _layout.tsx handles the auth/onboarding check
export default function Index() {
  return <Redirect href="/(tabs)/chat" />;
}
