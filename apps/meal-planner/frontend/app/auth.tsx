import { useState, useEffect } from "react";
import { Platform, View, Text, Pressable, ActivityIndicator } from "react-native";
import { useRouter } from "expo-router";
import {
  useAuthRequest,
  makeRedirectUri,
  exchangeCodeAsync,
  ResponseType,
  type DiscoveryDocument,
} from "expo-auth-session";
import * as WebBrowser from "expo-web-browser";
import { signInWithGoogle } from "../lib/api";
import { saveUserId } from "../lib/storage";

WebBrowser.maybeCompleteAuthSession();

const GOOGLE_DISCOVERY: DiscoveryDocument = {
  authorizationEndpoint: "https://accounts.google.com/o/oauth2/v2/auth",
  tokenEndpoint: "https://oauth2.googleapis.com/token",
  revocationEndpoint: "https://oauth2.googleapis.com/revoke",
};

const redirectUri = makeRedirectUri({ scheme: "com.nathanjones.mealplanner" });

export default function AuthScreen() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const iosClientId = process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID ?? "";
  const androidClientId = process.env.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID ?? "";
  const webClientId = process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID ?? "";

  const platformClientId =
    Platform.OS === "ios" ? iosClientId :
    Platform.OS === "android" ? androidClientId :
    webClientId;

  const [request, response, promptAsync] = useAuthRequest(
    {
      clientId: platformClientId,
      redirectUri,
      responseType: ResponseType.Code,
      scopes: ["openid", "profile", "email"],
      usePKCE: true,
    },
    GOOGLE_DISCOVERY
  );

  useEffect(() => {
    if (response?.type === "success") {
      const code = response.params.code;
      if (code) {
        exchangeCodeForToken(code);
      } else {
        setError("Google sign-in did not return an authorization code. Please try again.");
        setLoading(false);
      }
    } else if (response?.type === "error") {
      setError("Google sign-in failed. Please try again.");
      setLoading(false);
    } else if (response?.type === "dismiss") {
      setLoading(false);
    }
  }, [response]);

  async function exchangeCodeForToken(code: string) {
    if (!request?.codeVerifier) {
      setError("PKCE code verifier missing. Please try again.");
      setLoading(false);
      return;
    }
    try {
      const tokenResponse = await exchangeCodeAsync(
        {
          clientId: platformClientId,
          redirectUri,
          code,
          extraParams: { code_verifier: request.codeVerifier },
        },
        GOOGLE_DISCOVERY
      );
      await handleGoogleToken(tokenResponse.accessToken);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Token exchange failed. Please try again.");
      setLoading(false);
    }
  }

  async function handleGoogleToken(accessToken: string) {
    try {
      const result = await signInWithGoogle(accessToken);
      await saveUserId(result.user_id);
      if (result.is_new_user) {
        router.replace("/onboarding");
      } else {
        router.replace("/(tabs)/chat");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign-in failed. Please try again.");
      setLoading(false);
    }
  }

  function handleSignIn() {
    setError(null);
    setLoading(true);
    promptAsync();
  }

  const hasPlatformClientId = Boolean(platformClientId);

  return (
    <View className="flex-1 bg-white items-center justify-center px-8">
      {/* Logo / branding */}
      <View className="w-20 h-20 rounded-3xl bg-primary-600 items-center justify-center mb-6">
        <Text className="text-4xl">🥗</Text>
      </View>

      <Text className="text-3xl font-bold text-gray-900 mb-2">Meal Planner</Text>
      <Text className="text-gray-500 text-center mb-12 leading-relaxed">
        Your AI-powered meal planning assistant. Sign in to access your plans, chat history, and profile.
      </Text>

      {/* Google sign-in button */}
      <Pressable
        onPress={handleSignIn}
        disabled={loading || !request || !hasPlatformClientId}
        className="flex-row items-center justify-center gap-3 bg-white border border-gray-300 rounded-2xl px-6 py-4 w-full max-w-sm shadow-sm active:bg-gray-50"
        style={{ opacity: loading || !request || !hasPlatformClientId ? 0.6 : 1 }}
      >
        {loading ? (
          <ActivityIndicator color="#374151" />
        ) : (
          <>
            <View className="w-6 h-6 items-center justify-center">
              <Text style={{ fontSize: 18, fontWeight: "700", color: "#4285F4" }}>G</Text>
            </View>
            <Text className="text-gray-700 font-semibold text-base">Continue with Google</Text>
          </>
        )}
      </Pressable>

      {!hasPlatformClientId && (
        <Text className="text-orange-500 text-xs text-center mt-4 max-w-sm">
          Missing Google client ID. Set EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID / EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID / EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID and restart Metro.
        </Text>
      )}

      {error && (
        <Text className="text-red-500 text-sm text-center mt-4 max-w-sm">{error}</Text>
      )}

      <Text className="text-gray-400 text-xs text-center mt-8 max-w-xs leading-relaxed">
        By continuing, you agree to our Terms of Service and Privacy Policy.
      </Text>
    </View>
  );
}
