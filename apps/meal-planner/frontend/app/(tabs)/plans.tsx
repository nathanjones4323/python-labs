import { useEffect, useState, useCallback, useRef } from "react";
import {
  View,
  Text,
  ScrollView,
  Pressable,
  ActivityIndicator,
  RefreshControl,
} from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { Calendar, DateData } from "react-native-calendars";
import { listMealPlans, getMealPlan } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { MealPlan, MealPlanSummary, Meal } from "../../lib/types";
import MealCard from "../../components/MealCard";
import MacroBar from "../../components/MacroBar";

export default function PlansScreen() {
  const router = useRouter();
  const scrollRef = useRef<ScrollView>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<MealPlanSummary[]>([]);
  const [activePlan, setActivePlan] = useState<MealPlan | null>(null);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const dayOffsetsRef = useRef<Record<string, number>>({});

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
    dayOffsetsRef.current = {};
  }

  // Build calendar marked dates from active plan
  const markedDates: Record<string, { marked: boolean; dotColor: string; selected?: boolean; selectedColor?: string }> = {};
  if (activePlan) {
    for (const meal of activePlan.meals) {
      markedDates[meal.scheduled_date] = { marked: true, dotColor: "#16a34a" };
    }
  }
  if (selectedDate) {
    markedDates[selectedDate] = {
      ...markedDates[selectedDate],
      marked: true,
      dotColor: selectedDate === selectedDate ? "#ffffff" : "#16a34a",
      selected: true,
      selectedColor: "#16a34a",
    };
  }

  // Group meals by date for the full schedule view
  const mealsByDate: [string, Meal[]][] = activePlan
    ? Array.from(
        activePlan.meals.reduce((map, meal) => {
          const arr = map.get(meal.scheduled_date) ?? [];
          arr.push(meal);
          map.set(meal.scheduled_date, arr);
          return map;
        }, new Map<string, Meal[]>())
      ).sort(([a], [b]) => a.localeCompare(b))
    : [];

  function handleDayPress(day: DateData) {
    setSelectedDate(day.dateString);
    const offset = dayOffsetsRef.current[day.dateString];
    if (offset !== undefined) {
      scrollRef.current?.scrollTo({ y: offset - 8, animated: true });
    }
  }

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
      ref={scrollRef}
      className="flex-1 bg-white"
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); loadSummaries(); }} />
      }
    >
      {/* Active plan header + history toggle */}
      <View className="px-4 pt-4 pb-2 flex-row items-center justify-between border-b border-gray-100">
        <View className="flex-1 mr-3">
          <Text className="text-xs text-gray-400 uppercase tracking-wider font-medium mb-0.5">
            Active Plan
          </Text>
          <Text className="text-base font-semibold text-gray-900" numberOfLines={1}>
            {activePlan?.name ?? "—"}
          </Text>
          {activePlan && (
            <Text className="text-xs text-gray-400 mt-0.5">
              {new Date(activePlan.start_date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
              {activePlan.start_date !== activePlan.end_date
                ? " – " + new Date(activePlan.end_date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
                : ", " + new Date(activePlan.start_date + "T12:00:00").getFullYear()}
            </Text>
          )}
        </View>
        {summaries.length > 1 && (
          <Pressable
            onPress={() => setShowHistory((v) => !v)}
            className="flex-row items-center gap-1 bg-gray-100 px-3 py-2 rounded-xl"
          >
            <Ionicons name={showHistory ? "chevron-up" : "time-outline"} size={14} color="#6b7280" />
            <Text className="text-xs font-medium text-gray-600">
              {showHistory ? "Hide" : `History (${summaries.length})`}
            </Text>
          </Pressable>
        )}
      </View>

      {/* History list */}
      {showHistory && (
        <View className="border-b border-gray-100 bg-gray-50">
          {summaries.map((s) => {
            const isActive = activePlan?.id === s.id;
            const start = new Date(s.start_date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
            const end = new Date(s.end_date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
            const dateRange = s.start_date === s.end_date ? start + ", " + new Date(s.end_date + "T12:00:00").getFullYear() : `${start} – ${end}`;
            return (
              <Pressable
                key={s.id}
                onPress={() => { loadPlan(s.id); setShowHistory(false); }}
                className={`flex-row items-center px-4 py-3 border-b border-gray-100 ${isActive ? "bg-primary-50" : "active:bg-gray-100"}`}
              >
                <View className={`w-2 h-2 rounded-full mr-3 flex-shrink-0 ${isActive ? "bg-primary-600" : "bg-gray-300"}`} />
                <View className="flex-1">
                  <Text className={`text-sm font-medium ${isActive ? "text-primary-700" : "text-gray-800"}`} numberOfLines={1}>
                    {s.name}
                  </Text>
                  <Text className="text-xs text-gray-400 mt-0.5">
                    {dateRange} · {s.meal_count} meals
                  </Text>
                </View>
                {isActive && <Ionicons name="checkmark" size={16} color="#16a34a" />}
              </Pressable>
            );
          })}
        </View>
      )}

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
            onDayPress={handleDayPress}
            theme={{
              selectedDayBackgroundColor: "#16a34a",
              todayTextColor: "#16a34a",
              arrowColor: "#16a34a",
            }}
          />

          {/* Full schedule — all meals grouped by date */}
          <View className="px-4 pt-2 pb-6">
            <Text className="text-xs text-gray-500 font-medium uppercase tracking-wider mb-3 mt-2">
              Full Schedule
            </Text>
            {mealsByDate.map(([date, meals]) => {
              const isSelected = date === selectedDate;
              const label = new Date(date + "T12:00:00").toLocaleDateString("en-US", {
                weekday: "long",
                month: "long",
                day: "numeric",
              });
              return (
                <View
                  key={date}
                  onLayout={(e) => {
                    dayOffsetsRef.current[date] = e.nativeEvent.layout.y;
                  }}
                >
                  {/* Day header */}
                  <Pressable
                    onPress={() => setSelectedDate(date)}
                    className="flex-row items-center mb-2 mt-3"
                  >
                    <View
                      className={`w-2 h-2 rounded-full mr-2 ${isSelected ? "bg-primary-600" : "bg-gray-300"}`}
                    />
                    <Text
                      className={`text-sm font-semibold ${isSelected ? "text-primary-700" : "text-gray-700"}`}
                    >
                      {label}
                    </Text>
                  </Pressable>

                  {/* Meals for this day */}
                  {meals.map((meal) => (
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
                  ))}
                </View>
              );
            })}
          </View>
        </>
      )}
    </ScrollView>
  );
}
