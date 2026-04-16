import { useState, useRef, useEffect } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { chatStream } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { useRouter } from "expo-router";

interface Message {
  role: "user" | "assistant";
  content: string;
  isThinking?: boolean;
}

const WELCOME_MESSAGE: Message = {
  role: "assistant",
  content:
    "Hi! I'm your meal planning assistant. I'll create personalized meal plans using whole foods from the USDA database, complete with accurate nutritional info.\n\nTell me what you'd like — for example:\n- Create a 7-day meal plan for weight loss\n- Plan 5 days of high-protein meals, 3 meals a day\n- Make me a vegetarian meal plan for next week",
};

export default function ChatScreen() {
  const router = useRouter();
  const [messages, setMessages] = useState<Message[]>([WELCOME_MESSAGE]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);
  const scrollRef = useRef<ScrollView>(null);

  useEffect(() => {
    loadUserId().then(setUserId);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollToEnd({ animated: true });
  }, [messages]);

  async function send() {
    if (!input.trim() || streaming || !userId) return;
    const userMsg = input.trim();
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: userMsg },
      { role: "assistant", content: "", isThinking: true },
    ]);
    setStreaming(true);

    try {
      let assistantText = "";
      for await (const event of chatStream(userId, userMsg)) {
        if (event.type === "text") {
          assistantText += event.content ?? "";
          setMessages((prev) => [
            ...prev.slice(0, -1),
            { role: "assistant", content: assistantText },
          ]);
        } else if (event.type === "tool_use") {
          setMessages((prev) => [
            ...prev.slice(0, -1),
            {
              role: "assistant",
              content: assistantText,
              isThinking: true,
            },
          ]);
        } else if (event.type === "plan_saved" && event.plan_id) {
          setMessages((prev) => [
            ...prev.slice(0, -1),
            { role: "assistant", content: assistantText },
            {
              role: "assistant",
              content: `✅ Meal plan saved! [View plan →](/meal-plan/${event.plan_id})`,
            },
          ]);
          assistantText = "";
        }
      }
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last?.isThinking) {
          return [...prev.slice(0, -1), { role: "assistant", content: assistantText }];
        }
        return prev;
      });
    } catch (e: unknown) {
      setMessages((prev) => [
        ...prev.slice(0, -1),
        {
          role: "assistant",
          content: `Sorry, something went wrong: ${e instanceof Error ? e.message : "unknown error"}`,
        },
      ]);
    } finally {
      setStreaming(false);
    }
  }

  return (
    <KeyboardAvoidingView
      className="flex-1 bg-gray-50"
      behavior={Platform.OS === "ios" ? "padding" : "height"}
      keyboardVerticalOffset={88}
    >
      <ScrollView
        ref={scrollRef}
        className="flex-1 px-4 py-4"
        contentContainerStyle={{ paddingBottom: 16 }}
      >
        {messages.map((msg, i) => (
          <View
            key={i}
            className={`mb-3 max-w-[85%] ${msg.role === "user" ? "self-end" : "self-start"}`}
          >
            <View
              className={`rounded-2xl px-4 py-3 ${
                msg.role === "user"
                  ? "bg-primary-600 rounded-tr-sm"
                  : "bg-white border border-gray-100 rounded-tl-sm shadow-sm"
              }`}
            >
              {msg.isThinking && !msg.content ? (
                <View className="flex-row items-center gap-2">
                  <ActivityIndicator size="small" color="#16a34a" />
                  <Text className="text-gray-400 text-sm">Thinking…</Text>
                </View>
              ) : (
                <Text
                  className={`text-sm leading-relaxed ${
                    msg.role === "user" ? "text-white" : "text-gray-800"
                  }`}
                >
                  {msg.content}
                </Text>
              )}
            </View>
          </View>
        ))}
      </ScrollView>

      <View className="flex-row items-end px-4 py-3 bg-white border-t border-gray-100 gap-2">
        <TextInput
          className="flex-1 border border-gray-200 rounded-2xl px-4 py-2.5 text-sm text-gray-900 bg-gray-50 max-h-24"
          placeholder="Ask me to create a meal plan…"
          multiline
          value={input}
          onChangeText={setInput}
          returnKeyType="send"
          onSubmitEditing={send}
          editable={!streaming}
        />
        <Pressable
          onPress={send}
          disabled={streaming || !input.trim()}
          className={`w-10 h-10 rounded-full items-center justify-center ${
            streaming || !input.trim() ? "bg-gray-200" : "bg-primary-600"
          }`}
        >
          <Ionicons
            name="send"
            size={18}
            color={streaming || !input.trim() ? "#9ca3af" : "white"}
          />
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}
