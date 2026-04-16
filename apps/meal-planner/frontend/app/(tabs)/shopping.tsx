import { useEffect, useState, useCallback } from "react";
import {
  View,
  Text,
  ScrollView,
  Pressable,
  ActivityIndicator,
  RefreshControl,
} from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { listMealPlans, getMealPlan } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { MealPlan, MealPlanSummary } from "../../lib/types";
import ShoppingList from "../../components/ShoppingList";

export default function ShoppingScreen() {
  const router = useRouter();
  const [userId, setUserId] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<MealPlanSummary[]>([]);
  const [activePlan, setActivePlan] = useState<MealPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    loadUserId().then(setUserId);
  }, []);

  useFocusEffect(
    useCallback(() => {
      if (userId) load();
    }, [userId])
  );

  async function load() {
    if (!userId) return;
    try {
      const data = await listMealPlans(userId);
      setSummaries(data);
      if (data.length > 0) await selectPlan(data[0].id);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  async function selectPlan(planId: string) {
    const plan = await getMealPlan(planId);
    setActivePlan(plan);
  }

  if (loading) {
    return (
      <View className="flex-1 items-center justify-center">
        <ActivityIndicator color="#16a34a" />
      </View>
    );
  }

  if (!activePlan) {
    return (
      <View className="flex-1 items-center justify-center px-8">
        <Text className="text-4xl mb-4">🛒</Text>
        <Text className="text-xl font-bold text-gray-800 mb-2">No shopping list yet</Text>
        <Text className="text-gray-500 text-center mb-6">
          Create a meal plan first, then your shopping list will appear here.
        </Text>
        <Pressable
          onPress={() => router.push("/(tabs)/chat")}
          className="bg-primary-600 px-6 py-3 rounded-xl"
        >
          <Text className="text-white font-semibold">Create a Plan</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View
      className="flex-1 bg-white"
    >
      {/* Plan selector */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        className="px-4 py-3 border-b border-gray-100 flex-grow-0"
      >
        {summaries.map((s) => (
          <Pressable
            key={s.id}
            onPress={() => selectPlan(s.id)}
            className={`mr-2 px-4 py-2 rounded-full border ${
              activePlan.id === s.id
                ? "bg-primary-600 border-primary-600"
                : "bg-white border-gray-200"
            }`}
          >
            <Text
              className={`text-sm font-medium ${
                activePlan.id === s.id ? "text-white" : "text-gray-700"
              }`}
            >
              {s.name}
            </Text>
          </Pressable>
        ))}
      </ScrollView>

      <ScrollView
        className="flex-1 px-4 pt-4"
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => { setRefreshing(true); load(); }}
          />
        }
      >
        <ShoppingList plan={activePlan} />
      </ScrollView>
    </View>
  );
}
