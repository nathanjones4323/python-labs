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
import { loadUserId, loadSessionId, saveSessionId, clearSessionId } from "../../lib/storage";
import { useRouter } from "expo-router";
import MarkdownText from "../../components/MarkdownText";
import ChatHistorySidebar from "../../components/ChatHistorySidebar";

interface Message {
  role: "user" | "assistant";
  content: string;
  isThinking?: boolean;
  thinkingLabel?: string;
}

const SUGGESTIONS = [
  "1 day, 3 meals + 1 snack",
  "7-day weight loss plan",
  "High-protein, 5 days",
  "Vegetarian week",
];

export default function ChatScreen() {
  const router = useRouter();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [showSidebar, setShowSidebar] = useState(false);
  const scrollRef = useRef<ScrollView>(null);

  useEffect(() => {
    async function init() {
      const uid = await loadUserId();
      setUserId(uid);
      if (uid) {
        const sid = await loadSessionId();
        setSessionId(sid);
        // Restore the last session's messages if we have one
        if (sid) {
          try {
            const { getChatSession } = await import("../../lib/api");
            const session = await getChatSession(uid, sid);
            if (session.messages.length > 0) {
              setMessages(
                session.messages.map((m) => ({
                  role: m.role as "user" | "assistant",
                  content: m.content,
                }))
              );
            }
          } catch {
            // Session may have been deleted; start fresh
            await clearSessionId();
            setSessionId(null);
          }
        }
      }
    }
    init();
  }, []);

  useEffect(() => {
    if (messages.length > 0) {
      scrollRef.current?.scrollToEnd({ animated: true });
    }
  }, [messages]);

  function startNewChat() {
    setMessages([]);
    setSessionId(null);
    clearSessionId();
  }

  function loadHistorySession(sid: string, msgs: Array<{ role: string; content: string }>) {
    setSessionId(sid);
    saveSessionId(sid);
    setMessages(
      msgs.map((m) => ({
        role: m.role as "user" | "assistant",
        content: m.content,
      }))
    );
  }

  async function send(text?: string) {
    const userMsg = (text ?? input).trim();
    if (!userMsg || streaming || !userId) return;
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: userMsg },
      { role: "assistant", content: "", isThinking: true },
    ]);
    setStreaming(true);

    try {
      let assistantText = "";
      for await (const event of chatStream(userId, userMsg, sessionId)) {
        if (event.type === "session_created" && event.session_id) {
          setSessionId(event.session_id);
          saveSessionId(event.session_id);
        } else if (event.type === "text") {
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
              thinkingLabel: (event as { type: string; label?: string }).label,
            },
          ]);
        } else if (event.type === "plan_saved" && event.plan_id) {
          setMessages((prev) => [
            ...prev.slice(0, -1),
            ...(assistantText ? [{ role: "assistant" as const, content: assistantText }] : []),
            {
              role: "assistant" as const,
              content: `__PLAN_SAVED__${event.plan_id}`,
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

  const isEmpty = messages.length === 0;

  return (
    <KeyboardAvoidingView
      className="flex-1 bg-gray-50"
      behavior={Platform.OS === "ios" ? "padding" : "height"}
      keyboardVerticalOffset={88}
    >
      {/* Header buttons */}
      <View className="flex-row items-center justify-between px-4 pt-2 pb-1 bg-gray-50 border-b border-gray-100">
        <Pressable
          onPress={() => setShowSidebar(true)}
          className="p-2 rounded-xl active:bg-gray-200"
        >
          <Ionicons name="menu-outline" size={22} color="#6b7280" />
        </Pressable>
        {!isEmpty && (
          <Pressable
            onPress={startNewChat}
            className="flex-row items-center gap-1 px-3 py-1.5 rounded-xl bg-white border border-gray-200"
          >
            <Ionicons name="add" size={16} color="#16a34a" />
            <Text className="text-xs font-semibold text-primary-700">New Chat</Text>
          </Pressable>
        )}
      </View>

      <ScrollView
        ref={scrollRef}
        className="flex-1"
        contentContainerStyle={{ flexGrow: 1, paddingBottom: 16 }}
        keyboardShouldPersistTaps="handled"
      >
        {/* ── Empty / Welcome state ── */}
        {isEmpty && (
          <View className="flex-1 items-center justify-center px-6 pt-16 pb-8">
            {/* Icon badge */}
            <View className="w-20 h-20 rounded-3xl bg-primary-600 items-center justify-center mb-6 shadow-lg"
              style={{ shadowColor: "#16a34a", shadowOpacity: 0.35, shadowRadius: 16, shadowOffset: { width: 0, height: 8 } }}
            >
              <Text style={{ fontSize: 38 }}>🥗</Text>
            </View>

            <Text className="text-2xl font-bold text-gray-900 mb-2 text-center">
              Your Meal Planner
            </Text>
            <Text className="text-sm text-gray-500 text-center mb-8 leading-5">
              I'll build a personalized plan with real USDA nutrition data. Just tell me what you need.
            </Text>

            {/* Suggestion chips */}
            <View className="w-full gap-2">
              {SUGGESTIONS.map((s) => (
                <Pressable
                  key={s}
                  onPress={() => send(s)}
                  className="flex-row items-center bg-white border border-gray-200 rounded-2xl px-4 py-3.5 shadow-sm active:bg-gray-50"
                  style={{ shadowColor: "#000", shadowOpacity: 0.04, shadowRadius: 4, shadowOffset: { width: 0, height: 2 } }}
                >
                  <View className="w-8 h-8 rounded-xl bg-primary-50 items-center justify-center mr-3">
                    <Ionicons name="sparkles-outline" size={16} color="#16a34a" />
                  </View>
                  <Text className="text-sm font-medium text-gray-700 flex-1">{s}</Text>
                  <Ionicons name="chevron-forward" size={16} color="#d1d5db" />
                </Pressable>
              ))}
            </View>
          </View>
        )}

        {/* ── Message list ── */}
        {!isEmpty && (
          <View className="px-4 pt-4">
            {messages.map((msg, i) => {
              if (msg.content.startsWith("__PLAN_SAVED__")) {
                const planId = msg.content.replace("__PLAN_SAVED__", "");
                return (
                  <View key={i} className="items-center my-3">
                    <Pressable
                      onPress={() => router.push("/(tabs)/plans")}
                      className="flex-row items-center bg-primary-600 rounded-2xl px-5 py-3 gap-2 shadow-md"
                      style={{ shadowColor: "#16a34a", shadowOpacity: 0.3, shadowRadius: 8, shadowOffset: { width: 0, height: 4 } }}
                    >
                      <Ionicons name="checkmark-circle" size={20} color="white" />
                      <Text className="text-white font-semibold text-sm">Meal plan saved — View it</Text>
                      <Ionicons name="arrow-forward" size={16} color="white" />
                    </Pressable>
                  </View>
                );
              }

              const isUser = msg.role === "user";
              return (
                <View
                  key={i}
                  className={`mb-3 flex-row items-start gap-2 ${isUser ? "flex-row-reverse" : "flex-row"}`}
                >
                  {/* Avatar — assistant only, pinned to top */}
                  {!isUser && (
                    <View className="w-8 h-8 rounded-full bg-primary-100 items-center justify-center flex-shrink-0 mt-0.5">
                      <Text style={{ fontSize: 16 }}>🥗</Text>
                    </View>
                  )}

                  <View className={`max-w-[78%] ${isUser ? "items-end" : "items-start"}`}>
                    <View
                      className={`rounded-2xl px-4 py-3 ${
                        isUser
                          ? "bg-primary-600 rounded-tr-sm"
                          : "bg-white border border-gray-100 rounded-tl-sm shadow-sm"
                      }`}
                      style={
                        !isUser
                          ? { shadowColor: "#000", shadowOpacity: 0.05, shadowRadius: 4, shadowOffset: { width: 0, height: 2 } }
                          : undefined
                      }
                    >
                      {msg.isThinking && !msg.content ? (
                        <View className="flex-row items-center gap-2">
                          <ActivityIndicator size="small" color="#16a34a" />
                          <Text className="text-gray-400 text-sm italic">
                            {msg.thinkingLabel ?? "Thinking…"}
                          </Text>
                        </View>
                      ) : (
                        <>
                          <MarkdownText userMessage={isUser}>
                            {msg.content}
                          </MarkdownText>
                          {msg.isThinking && msg.content ? (
                            <Text className="text-primary-400 text-sm"> ●</Text>
                          ) : null}
                        </>
                      )}
                    </View>
                  </View>
                </View>
              );
            })}
          </View>
        )}
      </ScrollView>

      {/* ── Input bar ── */}
      <View
        className="bg-white border-t border-gray-100 px-4 py-3"
        style={{ shadowColor: "#000", shadowOpacity: 0.06, shadowRadius: 8, shadowOffset: { width: 0, height: -2 } }}
      >
        {/* Suggestion row when conversation has started */}
        {messages.length > 0 && messages.length <= 3 && (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            className="mb-2 -mx-1"
            contentContainerStyle={{ paddingHorizontal: 4, gap: 6 }}
          >
            {SUGGESTIONS.map((s) => (
              <Pressable
                key={s}
                onPress={() => send(s)}
                disabled={streaming}
                className="bg-primary-50 border border-primary-200 rounded-full px-3 py-1.5"
              >
                <Text className="text-xs font-medium text-primary-700">{s}</Text>
              </Pressable>
            ))}
          </ScrollView>
        )}

        <View className="flex-row items-end gap-3">
          <TextInput
            className="flex-1 border border-gray-200 rounded-2xl px-4 py-3 text-sm text-gray-900 bg-gray-50 max-h-28"
            placeholder="Describe the meal plan you want…"
            placeholderTextColor="#9ca3af"
            multiline
            value={input}
            onChangeText={setInput}
            returnKeyType={Platform.OS === "web" ? "default" : "send"}
            onSubmitEditing={Platform.OS !== "web" ? () => send() : undefined}
            onKeyPress={Platform.OS === "web" ? (e: any) => {
              if (e.nativeEvent.key === "Enter" && !e.nativeEvent.shiftKey) {
                e.preventDefault();
                send();
              }
            } : undefined}
            editable={!streaming}
          />
          <Pressable
            onPress={() => send()}
            disabled={streaming || !input.trim()}
            className={`w-11 h-11 rounded-2xl items-center justify-center ${
              streaming || !input.trim() ? "bg-gray-100" : "bg-primary-600"
            }`}
            style={
              !streaming && input.trim()
                ? { shadowColor: "#16a34a", shadowOpacity: 0.4, shadowRadius: 6, shadowOffset: { width: 0, height: 3 } }
                : undefined
            }
          >
            {streaming ? (
              <ActivityIndicator size="small" color="#9ca3af" />
            ) : (
              <Ionicons
                name="arrow-up"
                size={20}
                color={input.trim() ? "white" : "#d1d5db"}
              />
            )}
          </Pressable>
        </View>
      </View>

      {/* Chat history sidebar */}
      {userId && (
        <ChatHistorySidebar
          visible={showSidebar}
          userId={userId}
          currentSessionId={sessionId}
          onClose={() => setShowSidebar(false)}
          onSelectSession={loadHistorySession}
          onNewChat={startNewChat}
        />
      )}
    </KeyboardAvoidingView>
  );
}
