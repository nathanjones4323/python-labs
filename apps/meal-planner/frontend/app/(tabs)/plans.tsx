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
import { Calendar, DateData } from "react-native-calendars";
import { listMealPlans, getMealPlan } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { MealPlan, MealPlanSummary } from "../../lib/types";
import MealCard from "../../components/MealCard";
import MacroBar from "../../components/MacroBar";

export default function PlansScreen() {
  const router = useRouter();
  const [userId, setUserId] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<MealPlanSummary[]>([]);
  const [activePlan, setActivePlan] = useState<MealPlan | null>(null);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    loadUserId().then(setUserId);
  }, []);

  useFocusEffect(
    useCallback(() => {
      if (userId) loadSummaries();
    }, [userId])
  );

  async function loadSummaries() {
    if (!userId) return;
    try {
      const data = await listMealPlans(userId);
      setSummaries(data);
      if (data.length > 0) {
        await loadPlan(data[0].id);
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  async function loadPlan(planId: string) {
    const plan = await getMealPlan(planId);
    setActivePlan(plan);
    setSelectedDate(plan.start_date);
  }

  // Build calendar marked dates from active plan
  const markedDates: Record<string, { marked: boolean; dotColor: string }> = {};
  if (activePlan) {
    for (const meal of activePlan.meals) {
      markedDates[meal.scheduled_date] = { marked: true, dotColor: "#16a34a" };
    }
  }
  if (selectedDate) {
    markedDates[selectedDate] = {
      ...markedDates[selectedDate],
      marked: true,
      dotColor: "#16a34a",
    };
  }

  const mealsForDay = activePlan?.meals.filter(
    (m) => m.scheduled_date === selectedDate
  ) ?? [];

  if (loading) {
    return (
      <View className="flex-1 items-center justify-center bg-white">
        <ActivityIndicator color="#16a34a" />
      </View>
    );
  }

  if (summaries.length === 0) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8">
        <Text className="text-4xl mb-4">🥗</Text>
        <Text className="text-xl font-bold text-gray-800 mb-2">No meal plans yet</Text>
        <Text className="text-gray-500 text-center mb-6">
          Go to the Chat tab and ask me to create a meal plan for you.
        </Text>
        <Pressable
          onPress={() => router.push("/(tabs)/chat")}
          className="bg-primary-600 px-6 py-3 rounded-xl"
        >
          <Text className="text-white font-semibold">Start Planning</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <ScrollView
      className="flex-1 bg-white"
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); loadSummaries(); }} />
      }
    >
      {/* Plan selector */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        className="px-4 py-3 border-b border-gray-100"
      >
        {summaries.map((s) => (
          <Pressable
            key={s.id}
            onPress={() => loadPlan(s.id)}
            className={`mr-2 px-4 py-2 rounded-full border ${
              activePlan?.id === s.id
                ? "bg-primary-600 border-primary-600"
                : "bg-white border-gray-200"
            }`}
          >
            <Text
              className={`text-sm font-medium ${
                activePlan?.id === s.id ? "text-white" : "text-gray-700"
              }`}
            >
              {s.name}
            </Text>
          </Pressable>
        ))}
      </ScrollView>

      {activePlan && (
        <>
          {/* Macro summary */}
          <View className="px-4 py-3 bg-gray-50 border-b border-gray-100">
            <Text className="text-xs text-gray-500 mb-1 font-medium uppercase tracking-wider">
              Weekly totals · {activePlan.meals.length} meals
            </Text>
            <Text className="text-2xl font-bold text-gray-900 mb-2">
              {Math.round(activePlan.total_nutrition.calories).toLocaleString()} kcal
            </Text>
            <MacroBar nutrients={activePlan.total_nutrition} />
          </View>

          {/* Calendar */}
          <Calendar
            current={activePlan.start_date}
            minDate={activePlan.start_date}
            maxDate={activePlan.end_date}
            markedDates={markedDates}
            onDayPress={(day: DateData) => setSelectedDate(day.dateString)}
            theme={{
              selectedDayBackgroundColor: "#16a34a",
              todayTextColor: "#16a34a",
              arrowColor: "#16a34a",
            }}
          />

          {/* Meals for selected day */}
          <View className="px-4 py-3">
            {selectedDate ? (
              <>
                <Text className="text-base font-semibold text-gray-800 mb-3">
                  {new Date(selectedDate + "T12:00:00").toLocaleDateString("en-US", {
                    weekday: "long",
                    month: "long",
                    day: "numeric",
                  })}
                </Text>
                {mealsForDay.length === 0 ? (
                  <Text className="text-gray-400 text-sm">No meals scheduled for this day.</Text>
                ) : (
                  mealsForDay.map((meal) => (
                    <MealCard
                      key={meal.id}
                      meal={meal}
                      onPress={() =>
                        router.push({
                          pathname: "/meal/[id]",
                          params: { id: meal.id, planId: activePlan!.id },
                        })
                      }
                    />
                  ))
                )}
              </>
            ) : (
              <Text className="text-gray-400 text-sm">Select a day to view meals.</Text>
            )}
          </View>
        </>
      )}
    </ScrollView>
  );
}
