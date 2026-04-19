import { useEffect, useState } from "react";
import {
  View,
  Text,
  Pressable,
  ScrollView,
  Modal,
  ActivityIndicator,
  Alert,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { ChatSession } from "../lib/types";
import { listChatSessions, getChatSession, deleteChatSession } from "../lib/api";

interface Props {
  visible: boolean;
  userId: string;
  currentSessionId: string | null;
  onClose: () => void;
  onSelectSession: (sessionId: string, messages: Array<{ role: string; content: string }>) => void;
  onNewChat: () => void;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function ChatHistorySidebar({
  visible,
  userId,
  currentSessionId,
  onClose,
  onSelectSession,
  onNewChat,
}: Props) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingSession, setLoadingSession] = useState<string | null>(null);

  useEffect(() => {
    if (visible) {
      fetchSessions();
    }
  }, [visible]);

  async function fetchSessions() {
    setLoading(true);
    try {
      const data = await listChatSessions(userId);
      setSessions(data);
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  }

  async function handleSelect(session: ChatSession) {
    if (session.id === currentSessionId) {
      onClose();
      return;
    }
    setLoadingSession(session.id);
    try {
      const data = await getChatSession(userId, session.id);
      onSelectSession(session.id, data.messages);
      onClose();
    } catch {
      // silently fail
    } finally {
      setLoadingSession(null);
    }
  }

  function handleDelete(session: ChatSession) {
    Alert.alert(
      "Delete conversation",
      `Delete "${session.title}"?`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: async () => {
            try {
              await deleteChatSession(userId, session.id);
              setSessions((prev) => prev.filter((s) => s.id !== session.id));
            } catch {
              // silently fail
            }
          },
        },
      ]
    );
  }

  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={onClose}
    >
      <View className="flex-1 flex-row">
        {/* Sidebar panel */}
        <View className="w-72 bg-white h-full shadow-xl flex-col"
          style={{ shadowColor: "#000", shadowOpacity: 0.15, shadowRadius: 20, shadowOffset: { width: 4, height: 0 } }}
        >
          {/* Header */}
          <View className="px-4 pt-14 pb-3 border-b border-gray-100 flex-row items-center justify-between">
            <Text className="text-base font-bold text-gray-900">Chat History</Text>
            <Pressable onPress={onClose} className="p-1">
              <Ionicons name="close" size={22} color="#6b7280" />
            </Pressable>
          </View>

          {/* New chat button */}
          <Pressable
            onPress={() => { onNewChat(); onClose(); }}
            className="mx-4 my-3 flex-row items-center gap-2 bg-primary-600 px-4 py-3 rounded-xl"
          >
            <Ionicons name="add-circle-outline" size={18} color="white" />
            <Text className="text-white font-semibold text-sm">New Chat</Text>
          </Pressable>

          {/* Sessions list */}
          {loading ? (
            <View className="flex-1 items-center justify-center">
              <ActivityIndicator color="#16a34a" />
            </View>
          ) : sessions.length === 0 ? (
            <View className="flex-1 items-center justify-center px-6">
              <Text className="text-gray-400 text-sm text-center">No past conversations yet</Text>
            </View>
          ) : (
            <ScrollView className="flex-1">
              {sessions.map((session) => {
                const isActive = session.id === currentSessionId;
                const isLoadingThis = loadingSession === session.id;
                return (
                  <View
                    key={session.id}
                    className={`mx-2 mb-1 rounded-xl overflow-hidden ${isActive ? "bg-primary-50" : ""}`}
                  >
                    <Pressable
                      onPress={() => handleSelect(session)}
                      className="px-3 py-3 flex-row items-start"
                    >
                      <View className="flex-1 mr-2">
                        <Text
                          className={`text-sm font-medium leading-snug ${isActive ? "text-primary-700" : "text-gray-800"}`}
                          numberOfLines={2}
                        >
                          {session.title}
                        </Text>
                        <Text className="text-xs text-gray-400 mt-0.5">
                          {relativeTime(session.updated_at)} · {session.message_count} messages
                        </Text>
                      </View>
                      {isLoadingThis ? (
                        <ActivityIndicator size="small" color="#16a34a" />
                      ) : (
                        <Pressable
                          onPress={(e) => { e.stopPropagation?.(); handleDelete(session); }}
                          hitSlop={8}
                          className="p-1 mt-0.5"
                        >
                          <Ionicons name="trash-outline" size={15} color="#d1d5db" />
                        </Pressable>
                      )}
                    </Pressable>
                  </View>
                );
              })}
            </ScrollView>
          )}
        </View>

        {/* Dismiss overlay */}
        <Pressable className="flex-1 bg-black/40" onPress={onClose} />
      </View>
    </Modal>
  );
}
