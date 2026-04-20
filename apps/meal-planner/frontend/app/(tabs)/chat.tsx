import { useState, useRef, useEffect, useCallback } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Animated,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { chatStream, endChatSession } from "../../lib/api";
import { loadUserId, loadSessionId, saveSessionId, clearSessionId, loadPendingPrompt, clearPendingPrompt } from "../../lib/storage";
import { useRouter, useFocusEffect } from "expo-router";
import MarkdownText from "../../components/MarkdownText";
import ChatHistorySidebar from "../../components/ChatHistorySidebar";

interface ToolStep {
  name: string;
  label: string;
  status: "loading" | "done";
}

interface Message {
  role: "user" | "assistant";
  content: string;
  isThinking?: boolean;
  toolSteps?: ToolStep[];
  toolStepsExpanded?: boolean;
}

const SUGGESTIONS = [
  "1 day, 3 meals + 1 snack",
  "7-day weight loss plan",
  "High-protein, 5 days",
  "Vegetarian week",
];

function summarizeToolSteps(steps: ToolStep[]): string {
  const searchCount = steps.filter((s) => s.name === "search_foods").length;
  const nutritionCount = steps.filter((s) => s.name === "get_food_nutrition").length;
  const hasPlan = steps.some((s) => s.name === "create_meal_plan");
  const parts: string[] = [];
  if (searchCount > 0) parts.push(`searched ${searchCount} food${searchCount !== 1 ? "s" : ""}`);
  if (nutritionCount > 0) parts.push("looked up nutrition");
  if (hasPlan) parts.push("saved meal plan");
  return parts.length > 0 ? parts.join(", ") : "thinking";
}

function ShimmerText({ text }: { text: string }) {
  const opacity = useRef(new Animated.Value(0.3)).current;

  useEffect(() => {
    const anim = Animated.loop(
      Animated.sequence([
        Animated.timing(opacity, { toValue: 1, duration: 650, useNativeDriver: true }),
        Animated.timing(opacity, { toValue: 0.3, duration: 650, useNativeDriver: true }),
      ])
    );
    anim.start();
    return () => anim.stop();
  }, [opacity]);

  return (
    <Animated.Text style={{ opacity, fontSize: 12, color: "#6b7280" }}>
      {text}
    </Animated.Text>
  );
}

function ToolStepIcon({ name }: { name: string }) {
  const icons: Record<string, string> = {
    search_foods: "search-outline",
    get_food_nutrition: "analytics-outline",
    create_meal_plan: "calendar-outline",
  };
  return (
    <Ionicons
      name={(icons[name] ?? "ellipse-outline") as any}
      size={13}
      color="#9ca3af"
    />
  );
}

function ToolStepsView({
  steps,
  expanded,
  onToggle,
}: {
  steps: ToolStep[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const loadingStep = steps.find((s) => s.status === "loading");
  const allDone = steps.length > 0 && steps.every((s) => s.status === "done");
  const summary = summarizeToolSteps(steps);

  return (
    <View className="mb-2.5 rounded-xl border border-gray-100 overflow-hidden bg-gray-50">
      {/* Header row */}
      <Pressable
        onPress={onToggle}
        className="flex-row items-center gap-2 px-3 py-2 active:bg-gray-100"
      >
        <Text className="text-xs text-gray-500 flex-1" numberOfLines={1}>
          {summary}
        </Text>
        {loadingStep && !expanded && (
          <ShimmerText text={loadingStep.label} />
        )}
        <Ionicons
          name={expanded ? "chevron-down" : "chevron-forward"}
          size={12}
          color="#9ca3af"
        />
      </Pressable>

      {/* Expanded step list */}
      {expanded && (
        <View className="border-t border-gray-100 px-3 py-1.5">
          {steps.map((step, i) => (
            <View key={i} className="flex-row items-start gap-2 py-1">
              <View className="w-4 mt-0.5 items-center">
                <ToolStepIcon name={step.name} />
              </View>
              {step.status === "loading" ? (
                <ShimmerText text={step.label} />
              ) : (
                <Text className="text-xs text-gray-500 flex-1">{step.label}</Text>
              )}
            </View>
          ))}
          {allDone && (
            <View className="flex-row items-center gap-2 py-1">
              <View className="w-4 items-center">
                <Ionicons name="checkmark-circle-outline" size={13} color="#16a34a" />
              </View>
              <Text className="text-xs font-medium text-gray-600">Done</Text>
            </View>
          )}
        </View>
      )}
    </View>
  );
}

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

  useFocusEffect(
    useCallback(() => {
      async function checkPendingPrompt() {
        const pending = await loadPendingPrompt();
        if (pending) {
          await clearPendingPrompt();
          setInput(pending);
        }
      }
      checkPendingPrompt();
    }, [])
  );

  function startNewChat() {
    if (userId && sessionId) {
      endChatSession(userId, sessionId).catch(() => {});
    }
    setMessages([]);
    setSessionId(null);
    clearSessionId();
  }

  function loadHistorySession(sid: string, msgs: Array<{ role: string; content: string }>) {
    if (userId && sessionId && sessionId !== sid) {
      endChatSession(userId, sessionId).catch(() => {});
    }
    setSessionId(sid);
    saveSessionId(sid);
    setMessages(
      msgs.map((m) => ({
        role: m.role as "user" | "assistant",
        content: m.content,
      }))
    );
  }

  function toggleToolSteps(index: number) {
    setMessages((prev) =>
      prev.map((msg, i) =>
        i === index ? { ...msg, toolStepsExpanded: !msg.toolStepsExpanded } : msg
      )
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
      let toolSteps: ToolStep[] = [];

      const syncMessage = () => {
        setMessages((prev) => [
          ...prev.slice(0, -1),
          {
            role: "assistant" as const,
            content: assistantText,
            isThinking: false,
            toolSteps: toolSteps.length > 0 ? [...toolSteps] : undefined,
          },
        ]);
      };

      for await (const event of chatStream(userId, userMsg, sessionId)) {
        if (event.type === "session_created" && event.session_id) {
          setSessionId(event.session_id);
          saveSessionId(event.session_id);
        } else if (event.type === "text") {
          // Mark any loading step done when text begins streaming
          if (toolSteps.length > 0 && toolSteps[toolSteps.length - 1].status === "loading") {
            toolSteps = toolSteps.map((s, i) =>
              i === toolSteps.length - 1 ? { ...s, status: "done" as const } : s
            );
          }
          assistantText += event.content ?? "";
          syncMessage();
        } else if (event.type === "tool_use") {
          // Mark previous step done before starting the next
          if (toolSteps.length > 0 && toolSteps[toolSteps.length - 1].status === "loading") {
            toolSteps = toolSteps.map((s, i) =>
              i === toolSteps.length - 1 ? { ...s, status: "done" as const } : s
            );
          }
          toolSteps = [
            ...toolSteps,
            { name: event.name, label: event.label ?? event.name, status: "loading" },
          ];
          syncMessage();
        } else if (event.type === "plan_saved" && event.plan_id) {
          // Finalize all tool steps
          if (toolSteps.length > 0) {
            toolSteps = toolSteps.map((s) => ({ ...s, status: "done" as const }));
          }
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            const updatedLast: Message = {
              ...last,
              content: assistantText,
              isThinking: false,
              toolSteps: toolSteps.length > 0 ? [...toolSteps] : last.toolSteps,
            };
            return [
              ...prev.slice(0, -1),
              updatedLast,
              { role: "assistant" as const, content: `__PLAN_SAVED__${event.plan_id}` },
            ];
          });
          assistantText = "";
        }
      }

      // Finalize: mark all steps done, clear isThinking
      if (toolSteps.length > 0) {
        toolSteps = toolSteps.map((s) => ({ ...s, status: "done" as const }));
      }
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.content.startsWith("__PLAN_SAVED__")) return prev;
        return [
          ...prev.slice(0, -1),
          {
            ...last,
            content: assistantText || last.content,
            isThinking: false,
            toolSteps: toolSteps.length > 0 ? [...toolSteps] : last.toolSteps,
          },
        ];
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
                return (
                  <View key={i} className="items-start my-2">
                    <Pressable
                      onPress={() => router.push("/(tabs)/plans")}
                      className="flex-row items-center bg-primary-50 border border-primary-200 rounded-xl px-3 py-2 gap-1.5 active:bg-primary-100"
                    >
                      <Ionicons name="checkmark-circle-outline" size={14} color="#16a34a" />
                      <Text className="text-primary-700 font-medium text-xs">Meal plan saved — View it</Text>
                      <Ionicons name="chevron-forward" size={12} color="#16a34a" />
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
                  {/* Avatar — assistant only */}
                  {!isUser && (
                    <View className="w-8 h-8 rounded-full bg-primary-100 items-center justify-center flex-shrink-0 mt-0.5">
                      <Text style={{ fontSize: 16 }}>🥗</Text>
                    </View>
                  )}

                  <View className={`max-w-[78%] ${isUser ? "items-end" : "items-start"}`}>
                    {/* Tool steps accordion — shown above the text bubble for assistant */}
                    {!isUser && msg.toolSteps && msg.toolSteps.length > 0 && (
                      <ToolStepsView
                        steps={msg.toolSteps}
                        expanded={!!msg.toolStepsExpanded}
                        onToggle={() => toggleToolSteps(i)}
                      />
                    )}

                    {/* Chat bubble */}
                    {!!(msg.isThinking || msg.content) && (
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
                          // Initial spinner before first event
                          <View className="py-0.5">
                            <ActivityIndicator size="small" color="#16a34a" />
                          </View>
                        ) : (
                          <MarkdownText userMessage={isUser}>
                            {msg.content}
                          </MarkdownText>
                        )}
                      </View>
                    )}
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
