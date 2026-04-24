import { useEffect, useState, useCallback, useRef } from "react";
import {
  View,
  Text,
  ScrollView,
  Pressable,
  ActivityIndicator,
  RefreshControl,
  Modal,
  TextInput,
  Alert,
} from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { Calendar, DateData } from "react-native-calendars";
import { listMealPlans, getMealPlan, listFavoriteSummaries, favoriteMeal, unfavoriteMeal, aiPreviewMeal, updateMeal } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { MealPlan, MealPlanSummary, Meal, FavoriteMealSummary } from "../../lib/types";
import MealCard from "../../components/MealCard";
import MacroBar from "../../components/MacroBar";

interface NutrientRow {
  label: string;
  value: number | undefined;
  unit: string;
}

function NutrientDetails({ nutrients }: { nutrients: Record<string, number> }) {
  const [expanded, setExpanded] = useState(false);
  const rows: NutrientRow[] = [
    { label: "Fiber", value: nutrients.fiber_g, unit: "g" },
    { label: "Sugar", value: nutrients.sugar_g, unit: "g" },
    { label: "Saturated fat", value: nutrients.saturated_fat_g, unit: "g" },
    { label: "Cholesterol", value: nutrients.cholesterol_mg, unit: "mg" },
    { label: "Sodium", value: nutrients.sodium_mg, unit: "mg" },
    { label: "Potassium", value: nutrients.potassium_mg, unit: "mg" },
    { label: "Calcium", value: nutrients.calcium_mg, unit: "mg" },
    { label: "Iron", value: nutrients.iron_mg, unit: "mg" },
    { label: "Vitamin C", value: nutrients.vitamin_c_mg, unit: "mg" },
    { label: "Vitamin D", value: nutrients.vitamin_d_ug, unit: "μg" },
  ];
  return (
    <View className="mt-1">
      <Pressable
        onPress={() => setExpanded((v) => !v)}
        className="flex-row items-center gap-1 py-1"
      >
        <Text className="text-xs text-primary-600 font-medium">
          {expanded ? "Less detail" : "More nutrients"}
        </Text>
        <Ionicons name={expanded ? "chevron-up" : "chevron-down"} size={11} color="#16a34a" />
      </Pressable>
      {expanded && (
        <View className="mt-1 gap-0.5">
          {rows.map(({ label, value, unit }) => (
            <View key={label} className="flex-row justify-between py-0.5">
              <Text className="text-xs text-gray-500">{label}</Text>
              <Text className="text-xs font-medium text-gray-700">
                {value != null ? `${Math.round(value * 10) / 10} ${unit}` : "—"}
              </Text>
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

export default function PlansScreen() {
  const router = useRouter();
  const scrollRef = useRef<ScrollView>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<MealPlanSummary[]>([]);
  const [activePlan, setActivePlan] = useState<MealPlan | null>(null);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [favSummaries, setFavSummaries] = useState<FavoriteMealSummary[]>([]);
  const dayOffsetsRef = useRef<Record<string, number>>({});

  // Inline AI edit state
  const [aiEditTarget, setAiEditTarget] = useState<Meal | null>(null);
  const [aiInstruction, setAiInstruction] = useState("");
  const [aiLoading, setAiLoading] = useState(false);
  const [aiPreview, setAiPreview] = useState<Meal | null>(null);
  const [aiSaving, setAiSaving] = useState(false);

  useEffect(() => {
    loadUserId().then(setUserId);
  }, []);

  useFocusEffect(
    useCallback(() => {
      if (userId) {
        loadSummaries();
        loadFavorites(userId);
      }
    }, [userId])
  );

  async function loadFavorites(uid: string) {
    try {
      const data = await listFavoriteSummaries(uid);
      setFavSummaries(data);
    } catch {
      // silently fail
    }
  }

  async function handleFavorite(meal: Meal) {
    if (!userId) return;
    try {
      await favoriteMeal(userId, meal.id);
      await loadFavorites(userId);
    } catch {
      // silently fail
    }
  }

  async function handleUnfavorite(meal: Meal) {
    const existing = favSummaries.find((f) => f.source_meal_id === meal.id);
    if (!existing) return;
    try {
      await unfavoriteMeal(existing.id);
      setFavSummaries((prev) => prev.filter((f) => f.id !== existing.id));
    } catch {
      // silently fail
    }
  }

  function openAIEdit(meal: Meal) {
    setAiEditTarget(meal);
    setAiInstruction("");
    setAiPreview(null);
  }

  function closeAIEdit() {
    setAiEditTarget(null);
    setAiInstruction("");
    setAiPreview(null);
  }

  async function handleAIPreview() {
    if (!aiEditTarget || !userId || !aiInstruction.trim()) return;
    setAiLoading(true);
    try {
      const preview = await aiPreviewMeal(aiEditTarget.id, aiInstruction.trim(), userId);
      setAiPreview(preview);
    } catch (e) {
      Alert.alert("AI edit failed", e instanceof Error ? e.message : "Unknown error");
    } finally {
      setAiLoading(false);
    }
  }

  async function handleAIApply() {
    if (!aiPreview || !aiEditTarget) return;
    setAiSaving(true);
    try {
      const saved = await updateMeal(aiEditTarget.id, {
        name: aiPreview.name,
        instructions: aiPreview.instructions,
        foods: aiPreview.foods.map((f) => ({
          fdc_id: f.fdc_id,
          quantity_g: f.quantity_g,
          serving_description: f.serving_description ?? undefined,
        })),
      });
      // Update the meal in activePlan state
      setActivePlan((prev) =>
        prev
          ? { ...prev, meals: prev.meals.map((m) => (m.id === saved.id ? saved : m)) }
          : prev
      );
      closeAIEdit();
    } catch (e) {
      Alert.alert("Save failed", e instanceof Error ? e.message : "Unknown error");
    } finally {
      setAiSaving(false);
    }
  }

  async function loadSummaries() {
    if (!userId) return;
    setError(null);
    try {
      const data = await listMealPlans(userId);
      setSummaries(data);
      if (data.length > 0) {
        await loadPlan(data[0].id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load meal plans");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  async function loadPlan(planId: string) {
    setError(null);
    try {
      const plan = await getMealPlan(planId);
      setActivePlan(plan);
      setSelectedDate(plan.start_date);
      dayOffsetsRef.current = {};
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load plan");
    }
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

  // Daily average nutrition
  const numDays = mealsByDate.length || 1;
  const dailyAvg = activePlan ? {
    calories: activePlan.total_nutrition.calories / numDays,
    protein_g: activePlan.total_nutrition.protein_g / numDays,
    carbs_g: activePlan.total_nutrition.carbs_g / numDays,
    fat_g: activePlan.total_nutrition.fat_g / numDays,
    fiber_g: activePlan.total_nutrition.fiber_g / numDays,
    sugar_g: activePlan.total_nutrition.sugar_g / numDays,
    saturated_fat_g: activePlan.total_nutrition.saturated_fat_g / numDays,
    cholesterol_mg: activePlan.total_nutrition.cholesterol_mg / numDays,
    sodium_mg: activePlan.total_nutrition.sodium_mg / numDays,
    calcium_mg: activePlan.total_nutrition.calcium_mg / numDays,
    iron_mg: activePlan.total_nutrition.iron_mg / numDays,
    magnesium_mg: activePlan.total_nutrition.magnesium_mg / numDays,
    phosphorus_mg: activePlan.total_nutrition.phosphorus_mg / numDays,
    potassium_mg: activePlan.total_nutrition.potassium_mg / numDays,
    zinc_mg: activePlan.total_nutrition.zinc_mg / numDays,
    vitamin_a_ug: activePlan.total_nutrition.vitamin_a_ug / numDays,
    vitamin_c_mg: activePlan.total_nutrition.vitamin_c_mg / numDays,
    vitamin_d_ug: activePlan.total_nutrition.vitamin_d_ug / numDays,
    vitamin_e_mg: activePlan.total_nutrition.vitamin_e_mg / numDays,
    vitamin_k_ug: activePlan.total_nutrition.vitamin_k_ug / numDays,
    vitamin_b6_mg: activePlan.total_nutrition.vitamin_b6_mg / numDays,
    vitamin_b12_ug: activePlan.total_nutrition.vitamin_b12_ug / numDays,
    folate_ug: activePlan.total_nutrition.folate_ug / numDays,
    thiamin_mg: activePlan.total_nutrition.thiamin_mg / numDays,
    riboflavin_mg: activePlan.total_nutrition.riboflavin_mg / numDays,
    niacin_mg: activePlan.total_nutrition.niacin_mg / numDays,
  } : null;

  // Selected day totals
  const selectedMeals = activePlan?.meals.filter(m => m.scheduled_date === selectedDate) ?? [];
  const dayTotals = selectedMeals.length > 0 ? selectedMeals.reduce(
    (acc, meal) => ({
      calories: acc.calories + meal.total_nutrition.calories,
      protein_g: acc.protein_g + meal.total_nutrition.protein_g,
      carbs_g: acc.carbs_g + meal.total_nutrition.carbs_g,
      fat_g: acc.fat_g + meal.total_nutrition.fat_g,
      fiber_g: acc.fiber_g + meal.total_nutrition.fiber_g,
      sugar_g: acc.sugar_g + meal.total_nutrition.sugar_g,
      saturated_fat_g: acc.saturated_fat_g + meal.total_nutrition.saturated_fat_g,
      cholesterol_mg: acc.cholesterol_mg + meal.total_nutrition.cholesterol_mg,
      sodium_mg: acc.sodium_mg + meal.total_nutrition.sodium_mg,
      calcium_mg: acc.calcium_mg + meal.total_nutrition.calcium_mg,
      iron_mg: acc.iron_mg + meal.total_nutrition.iron_mg,
      magnesium_mg: acc.magnesium_mg + meal.total_nutrition.magnesium_mg,
      phosphorus_mg: acc.phosphorus_mg + meal.total_nutrition.phosphorus_mg,
      potassium_mg: acc.potassium_mg + meal.total_nutrition.potassium_mg,
      zinc_mg: acc.zinc_mg + meal.total_nutrition.zinc_mg,
      vitamin_a_ug: acc.vitamin_a_ug + meal.total_nutrition.vitamin_a_ug,
      vitamin_c_mg: acc.vitamin_c_mg + meal.total_nutrition.vitamin_c_mg,
      vitamin_d_ug: acc.vitamin_d_ug + meal.total_nutrition.vitamin_d_ug,
      vitamin_e_mg: acc.vitamin_e_mg + meal.total_nutrition.vitamin_e_mg,
      vitamin_k_ug: acc.vitamin_k_ug + meal.total_nutrition.vitamin_k_ug,
      vitamin_b6_mg: acc.vitamin_b6_mg + meal.total_nutrition.vitamin_b6_mg,
      vitamin_b12_ug: acc.vitamin_b12_ug + meal.total_nutrition.vitamin_b12_ug,
      folate_ug: acc.folate_ug + meal.total_nutrition.folate_ug,
      thiamin_mg: acc.thiamin_mg + meal.total_nutrition.thiamin_mg,
      riboflavin_mg: acc.riboflavin_mg + meal.total_nutrition.riboflavin_mg,
      niacin_mg: acc.niacin_mg + meal.total_nutrition.niacin_mg,
    }),
    {
      calories: 0, protein_g: 0, carbs_g: 0, fat_g: 0, fiber_g: 0, sugar_g: 0,
      saturated_fat_g: 0, cholesterol_mg: 0, sodium_mg: 0, calcium_mg: 0, iron_mg: 0,
      magnesium_mg: 0, phosphorus_mg: 0, potassium_mg: 0, zinc_mg: 0, vitamin_a_ug: 0,
      vitamin_c_mg: 0, vitamin_d_ug: 0, vitamin_e_mg: 0, vitamin_k_ug: 0, vitamin_b6_mg: 0,
      vitamin_b12_ug: 0, folate_ug: 0, thiamin_mg: 0, riboflavin_mg: 0, niacin_mg: 0,
    }
  ) : null;

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

  if (error && summaries.length === 0) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8">
        <View className="w-14 h-14 rounded-2xl bg-red-50 items-center justify-center mb-4">
          <Ionicons name="cloud-offline-outline" size={28} color="#ef4444" />
        </View>
        <Text className="text-base font-semibold text-gray-800 mb-1 text-center">Couldn't load</Text>
        <Text className="text-sm text-gray-500 text-center mb-5">{error}</Text>
        <Pressable
          onPress={() => { setLoading(true); loadSummaries(); }}
          className="flex-row items-center gap-2 bg-primary-600 px-5 py-2.5 rounded-xl"
        >
          <Ionicons name="refresh" size={16} color="white" />
          <Text className="text-white font-semibold text-sm">Try Again</Text>
        </Pressable>
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
    <>
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
          {summaries.find(s => s.id === activePlan?.id)?.created_at && (
            <Text className="text-xs text-gray-400 mt-0.5">
              {(() => {
                const s = summaries.find(s => s.id === activePlan?.id)!;
                return `Created ${new Date(s.created_at!).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}${s.conversation_title ? ` · from "${s.conversation_title}"` : ""}`;
              })()}
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
                  {s.created_at && (
                    <Text className="text-xs text-gray-400 mt-0.5" numberOfLines={1}>
                      Created {new Date(s.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                      {s.conversation_title ? ` · "${s.conversation_title}"` : ""}
                    </Text>
                  )}
                </View>
                {isActive && <Ionicons name="checkmark" size={16} color="#16a34a" />}
              </Pressable>
            );
          })}
        </View>
      )}

      {activePlan && (
        <>
          {/* Macro summary — daily averages */}
          <View className="px-4 py-3 bg-gray-50 border-b border-gray-100">
            <Text className="text-xs text-gray-500 mb-1 font-medium uppercase tracking-wider">
              Daily avg · {numDays} {numDays === 1 ? "day" : "days"}
            </Text>
            <Text className="text-2xl font-bold text-gray-900 mb-2">
              {dailyAvg ? Math.round(dailyAvg.calories).toLocaleString() : "—"} kcal
            </Text>
            {dailyAvg && <MacroBar nutrients={dailyAvg} />}
            {dailyAvg && <NutrientDetails nutrients={dailyAvg} />}
          </View>

          {/* Selected day totals */}
          {dayTotals && selectedDate && (
            <View className="px-4 py-3 border-b border-gray-100">
              <Text className="text-xs text-gray-500 mb-1 font-medium uppercase tracking-wider">
                {new Date(selectedDate + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })} · {selectedMeals.length} {selectedMeals.length === 1 ? "meal" : "meals"}
              </Text>
              <Text className="text-xl font-bold text-gray-900 mb-2">
                {Math.round(dayTotals.calories).toLocaleString()} kcal
              </Text>
              <MacroBar nutrients={dayTotals} />
              <NutrientDetails nutrients={dayTotals} />
            </View>
          )}

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
                  {meals.map((meal) => {
                    const existingFav = favSummaries.find((f) => f.source_meal_id === meal.id);
                    return (
                      <View key={meal.id}>
                        <MealCard
                          meal={meal}
                          onPress={() =>
                            router.push({
                              pathname: "/meal/[id]",
                              params: { id: meal.id, planId: activePlan!.id },
                            })
                          }
                          isFavorited={!!existingFav}
                          favoriteId={existingFav?.id}
                          onFavorite={() => handleFavorite(meal)}
                          onUnfavorite={() => handleUnfavorite(meal)}
                        />
                        <Pressable
                          onPress={() => openAIEdit(meal)}
                          className="flex-row items-center gap-1.5 self-start ml-1 mb-2 px-3 py-1.5 rounded-full bg-primary-50 border border-primary-100 active:bg-primary-100"
                        >
                          <Ionicons name="sparkles-outline" size={12} color="#16a34a" />
                          <Text className="text-xs font-medium text-primary-700">Edit with AI</Text>
                        </Pressable>
                      </View>
                    );
                  })}
                </View>
              );
            })}
          </View>
        </>
      )}
    </ScrollView>

      {/* Inline AI edit modal */}
      <Modal
        visible={!!aiEditTarget}
        transparent
        animationType="slide"
        onRequestClose={closeAIEdit}
      >
        <View style={{ flex: 1, justifyContent: "flex-end", backgroundColor: "rgba(0,0,0,0.4)" }}>
          <View style={{ backgroundColor: "white", borderTopLeftRadius: 24, borderTopRightRadius: 24, paddingHorizontal: 16, paddingTop: 16, paddingBottom: 32 }}>
            <View style={{ alignItems: "center", marginBottom: 12 }}>
              <View style={{ width: 40, height: 4, borderRadius: 2, backgroundColor: "#e5e7eb" }} />
            </View>

            {!aiPreview ? (
              <>
                <View style={{ flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <Ionicons name="sparkles-outline" size={18} color="#16a34a" />
                  <Text style={{ fontSize: 15, fontWeight: "700", color: "#111827" }}>Edit with AI</Text>
                </View>
                {aiEditTarget && (
                  <Text style={{ fontSize: 12, color: "#9ca3af", marginBottom: 10 }}>{aiEditTarget.name}</Text>
                )}
                <Text style={{ fontSize: 13, color: "#6b7280", marginBottom: 10 }}>
                  Describe what to change — e.g. "replace chicken with salmon" or "double the protein"
                </Text>
                <TextInput
                  style={{ borderWidth: 1, borderColor: "#e5e7eb", borderRadius: 12, paddingHorizontal: 14, paddingVertical: 12, fontSize: 14, color: "#111827", backgroundColor: "#f9fafb", minHeight: 80, textAlignVertical: "top" }}
                  placeholder="What would you like to change?"
                  placeholderTextColor="#9ca3af"
                  multiline
                  value={aiInstruction}
                  onChangeText={setAiInstruction}
                  editable={!aiLoading}
                />
                <View style={{ flexDirection: "row", gap: 10, marginTop: 14 }}>
                  <Pressable onPress={closeAIEdit} disabled={aiLoading} style={{ flex: 1, borderWidth: 1, borderColor: "#d1d5db", borderRadius: 12, paddingVertical: 12, alignItems: "center" }}>
                    <Text style={{ fontSize: 14, fontWeight: "600", color: "#374151" }}>Cancel</Text>
                  </Pressable>
                  <Pressable
                    onPress={handleAIPreview}
                    disabled={aiLoading || !aiInstruction.trim()}
                    style={{ flex: 1, borderRadius: 12, paddingVertical: 12, alignItems: "center", backgroundColor: aiLoading || !aiInstruction.trim() ? "#e5e7eb" : "#16a34a" }}
                  >
                    {aiLoading ? (
                      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
                        <ActivityIndicator size="small" color="#16a34a" />
                        <Text style={{ fontSize: 14, fontWeight: "600", color: "#6b7280" }}>Editing…</Text>
                      </View>
                    ) : (
                      <Text style={{ fontSize: 14, fontWeight: "600", color: aiInstruction.trim() ? "white" : "#9ca3af" }}>Preview Edit</Text>
                    )}
                  </Pressable>
                </View>
              </>
            ) : (
              /* Diff preview */
              <>
                {(() => {
                  if (!aiEditTarget || !aiPreview) return null;
                  const oldN = aiEditTarget.total_nutrition;
                  const newN = aiPreview.total_nutrition;
                  const calDiff = Math.round(newN.calories - oldN.calories);
                  const protDiff = parseFloat((newN.protein_g - oldN.protein_g).toFixed(1));
                  const carbDiff = parseFloat((newN.carbs_g - oldN.carbs_g).toFixed(1));
                  const fatDiff  = parseFloat((newN.fat_g  - oldN.fat_g).toFixed(1));
                  const fmtD = (d: number, u: string) => d === 0 ? `0${u}` : d > 0 ? `+${d}${u}` : `${d}${u}`;
                  const dStyle = (d: number) => ({ color: d > 0 ? "#dc2626" : d < 0 ? "#15803d" : "#9ca3af" });

                  const oldById = new Map(aiEditTarget.foods.map((f) => [f.fdc_id, f]));
                  const newById = new Map(aiPreview.foods.map((f) => [f.fdc_id, f]));
                  const diffFoods: { fdc_id: number; desc: string; status: "added"|"removed"|"changed"|"same"; qty: number; oldQty?: number }[] = [];
                  for (const f of aiEditTarget.foods) {
                    const nf = newById.get(f.fdc_id);
                    if (!nf) diffFoods.push({ fdc_id: f.fdc_id, desc: f.description, status: "removed", qty: f.quantity_g });
                    else if (Math.round(nf.quantity_g) !== Math.round(f.quantity_g))
                      diffFoods.push({ fdc_id: f.fdc_id, desc: nf.description, status: "changed", qty: nf.quantity_g, oldQty: f.quantity_g });
                    else diffFoods.push({ fdc_id: f.fdc_id, desc: nf.description, status: "same", qty: nf.quantity_g });
                  }
                  for (const f of aiPreview.foods) {
                    if (!oldById.has(f.fdc_id)) diffFoods.push({ fdc_id: f.fdc_id, desc: f.description, status: "added", qty: f.quantity_g });
                  }

                  return (
                    <ScrollView style={{ maxHeight: 480 }} showsVerticalScrollIndicator={false}>
                      {/* Header */}
                      <View style={{ backgroundColor: "#111827", borderRadius: 12, paddingHorizontal: 14, paddingVertical: 10, flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 10 }}>
                        <Ionicons name="sparkles" size={14} color="#a3e635" />
                        <Text style={{ fontSize: 13, fontWeight: "700", color: "white", flex: 1 }}>AI Edit Preview</Text>
                        <Text style={{ fontSize: 11, color: "#9ca3af" }} numberOfLines={1}>{aiPreview.name}</Text>
                      </View>

                      {/* Nutrition delta */}
                      <View style={{ flexDirection: "row", gap: 8, marginBottom: 12 }}>
                        {[
                          { label: "Calories", val: `${Math.round(newN.calories)}`, delta: fmtD(calDiff, " kcal"), ds: dStyle(calDiff), vc: "#111827" },
                          { label: "Protein",  val: `${newN.protein_g.toFixed(1)}g`, delta: fmtD(protDiff, "g"), ds: dStyle(protDiff), vc: "#1d4ed8" },
                          { label: "Carbs",    val: `${newN.carbs_g.toFixed(1)}g`, delta: fmtD(carbDiff, "g"), ds: dStyle(carbDiff), vc: "#ea580c" },
                          { label: "Fat",      val: `${newN.fat_g.toFixed(1)}g`, delta: fmtD(fatDiff, "g"), ds: dStyle(fatDiff), vc: "#ca8a04" },
                        ].map((m) => (
                          <View key={m.label} style={{ flex: 1, backgroundColor: "#f9fafb", borderWidth: 1, borderColor: "#e5e7eb", borderRadius: 10, padding: 8, alignItems: "center" }}>
                            <Text style={{ fontSize: 10, color: "#9ca3af", marginBottom: 2 }}>{m.label}</Text>
                            <Text style={{ fontSize: 14, fontWeight: "800", color: m.vc }}>{m.val}</Text>
                            <Text style={[{ fontSize: 11, fontWeight: "700" }, m.ds]}>{m.delta}</Text>
                          </View>
                        ))}
                      </View>

                      {/* Ingredient diff */}
                      <Text style={{ fontSize: 10, fontWeight: "700", color: "#6b7280", textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 6 }}>Ingredients</Text>
                      {diffFoods.map((item, i) => {
                        const bg = item.status === "removed" ? "#fef2f2" : item.status === "added" ? "#f0fdf4" : item.status === "changed" ? "#fffbeb" : "transparent";
                        const border = item.status === "removed" ? "#ef4444" : item.status === "added" ? "#22c55e" : item.status === "changed" ? "#f59e0b" : "transparent";
                        const textColor = item.status === "removed" ? "#b91c1c" : item.status === "added" ? "#15803d" : item.status === "changed" ? "#92400e" : "#9ca3af";
                        const badge = item.status === "removed" ? "−" : item.status === "added" ? "+" : item.status === "changed" ? "~" : null;
                        const badgeBg = item.status === "removed" ? "#ef4444" : item.status === "added" ? "#22c55e" : "#f59e0b";
                        return (
                          <View key={i} style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginBottom: 4, borderRadius: 8, backgroundColor: bg, borderLeftWidth: item.status !== "same" ? 4 : 0, borderLeftColor: border, paddingHorizontal: 10, paddingVertical: 8 }}>
                            <View style={{ flexDirection: "row", alignItems: "center", gap: 8, flex: 1 }}>
                              {badge && (
                                <View style={{ width: 18, height: 18, borderRadius: 9, backgroundColor: badgeBg, alignItems: "center", justifyContent: "center" }}>
                                  <Text style={{ color: "white", fontSize: 12, fontWeight: "800", lineHeight: 18 }}>{badge}</Text>
                                </View>
                              )}
                              <Text style={{ fontSize: 13, fontWeight: item.status !== "same" ? "600" : "400", color: textColor, textDecorationLine: item.status === "removed" ? "line-through" : "none", flex: 1 }}>{item.desc}</Text>
                            </View>
                            <Text style={{ fontSize: 12, color: textColor, marginLeft: 8 }}>
                              {item.status === "changed" && item.oldQty !== undefined ? `${Math.round(item.oldQty)}g → ` : ""}{Math.round(item.qty)}g
                            </Text>
                          </View>
                        );
                      })}

                      <View style={{ flexDirection: "row", gap: 10, marginTop: 14 }}>
                        <Pressable onPress={() => setAiPreview(null)} disabled={aiSaving} style={{ flex: 1, borderWidth: 1, borderColor: "#d1d5db", borderRadius: 12, paddingVertical: 12, alignItems: "center" }}>
                          <Text style={{ fontSize: 14, fontWeight: "600", color: "#374151" }}>← Back</Text>
                        </Pressable>
                        <Pressable onPress={handleAIApply} disabled={aiSaving} style={{ flex: 1, backgroundColor: "#16a34a", borderRadius: 12, paddingVertical: 12, alignItems: "center" }}>
                          {aiSaving
                            ? <ActivityIndicator size="small" color="white" />
                            : <Text style={{ fontSize: 14, fontWeight: "600", color: "white" }}>Apply Changes</Text>
                          }
                        </Pressable>
                      </View>
                    </ScrollView>
                  );
                })()}
              </>
            )}
          </View>
        </View>
      </Modal>
      </>
  );
}
