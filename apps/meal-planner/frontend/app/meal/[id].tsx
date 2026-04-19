import { useEffect, useState } from "react";
import {
  View,
  Text,
  ScrollView,
  ActivityIndicator,
  Pressable,
  TextInput,
  Modal,
  Alert,
} from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { getMealPlan, updateMeal, aiEditMeal } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { Meal, MealPlan, MealFood, FoodSearchResult } from "../../lib/types";
import NutritionLabel from "../../components/NutritionLabel";
import MacroBar from "../../components/MacroBar";
import FoodSearchModal from "../../components/FoodSearchModal";

export default function MealDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const planId = useLocalSearchParams<{ planId?: string }>().planId;

  const [meal, setMeal] = useState<Meal | null>(null);
  const [loading, setLoading] = useState(true);
  const [showNutrition, setShowNutrition] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);

  // Manual edit state
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editInstructions, setEditInstructions] = useState<string[]>([]);
  const [editFoods, setEditFoods] = useState<MealFood[]>([]);
  const [editQuantities, setEditQuantities] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [showFoodSearch, setShowFoodSearch] = useState(false);

  // AI edit state
  const [showAIEdit, setShowAIEdit] = useState(false);
  const [aiInstruction, setAIInstruction] = useState("");
  const [aiLoading, setAILoading] = useState(false);
  const [aiResult, setAiResult] = useState<Meal | null>(null); // pending diff

  useEffect(() => {
    loadUserId().then(setUserId);
  }, []);

  useEffect(() => {
    if (!planId) { router.back(); return; }
    getMealPlan(planId)
      .then((plan: MealPlan) => {
        const found = plan.meals.find((m) => m.id === id);
        setMeal(found ?? null);
        if (found) initEditState(found);
      })
      .finally(() => setLoading(false));
  }, [id, planId]);

  function initEditState(m: Meal) {
    setEditName(m.name);
    setEditInstructions([...m.instructions]);
    setEditFoods([...m.foods]);
    const qtys: Record<string, string> = {};
    for (const f of m.foods) qtys[f.id] = String(Math.round(f.quantity_g));
    setEditQuantities(qtys);
  }

  function enterEditMode() {
    if (meal) initEditState(meal);
    setEditing(true);
  }

  function cancelEdit() {
    setEditing(false);
    if (meal) initEditState(meal);
  }

  async function saveEdits() {
    if (!meal) return;
    setSaving(true);
    try {
      const foods = editFoods.map((f) => ({
        fdc_id: f.fdc_id,
        quantity_g: parseFloat(editQuantities[f.id] ?? String(f.quantity_g)) || f.quantity_g,
        serving_description: f.serving_description,
      }));
      const updated = await updateMeal(meal.id, {
        name: editName,
        instructions: editInstructions,
        foods,
      });
      setMeal(updated);
      initEditState(updated);
      setEditing(false);
    } catch (e) {
      Alert.alert("Save failed", e instanceof Error ? e.message : "Unknown error");
    } finally {
      setSaving(false);
    }
  }

  function addEditFood(food: FoodSearchResult & { quantity_g: number }) {
    const tempId = `new-${food.fdc_id}-${Date.now()}`;
    const newFood: MealFood = {
      id: tempId,
      fdc_id: food.fdc_id,
      description: food.description,
      quantity_g: food.quantity_g,
      serving_description: null,
      nutrients: {
        calories: 0, protein_g: 0, fat_g: 0, carbs_g: 0, fiber_g: 0,
        sugar_g: 0, saturated_fat_g: 0, cholesterol_mg: 0, sodium_mg: 0,
        calcium_mg: 0, iron_mg: 0, magnesium_mg: 0, phosphorus_mg: 0,
        potassium_mg: 0, zinc_mg: 0, vitamin_a_ug: 0, vitamin_c_mg: 0,
        vitamin_d_ug: 0, vitamin_e_mg: 0, vitamin_k_ug: 0, vitamin_b6_mg: 0,
        vitamin_b12_ug: 0, folate_ug: 0, thiamin_mg: 0, riboflavin_mg: 0, niacin_mg: 0,
      },
    };
    setEditFoods((prev) => [...prev, newFood]);
    setEditQuantities((prev) => ({ ...prev, [tempId]: String(food.quantity_g) }));
  }

  function removeEditFood(foodId: string) {
    setEditFoods((prev) => prev.filter((f) => f.id !== foodId));
    setEditQuantities((prev) => { const copy = { ...prev }; delete copy[foodId]; return copy; });
  }

  async function handleAIEdit() {
    if (!meal || !userId || !aiInstruction.trim()) return;
    setAILoading(true);
    try {
      const updated = await aiEditMeal(meal.id, aiInstruction.trim(), userId);
      setAiResult(updated);
      setShowAIEdit(false);
    } catch (e) {
      Alert.alert("AI edit failed", e instanceof Error ? e.message : "Unknown error");
    } finally {
      setAILoading(false);
    }
  }

  function applyAIResult() {
    if (!aiResult) return;
    setMeal(aiResult);
    initEditState(aiResult);
    setAiResult(null);
    setAIInstruction("");
  }

  function discardAIResult() {
    setAiResult(null);
  }

  if (loading) {
    return <View className="flex-1 items-center justify-center"><ActivityIndicator color="#16a34a" /></View>;
  }
  if (!meal) {
    return <View className="flex-1 items-center justify-center px-8"><Text className="text-gray-500">Meal not found.</Text></View>;
  }

  const cal = Math.round(meal.total_nutrition.calories);

  // Compute diff between meal.foods and aiResult.foods
  const diffFoods = aiResult ? (() => {
    const oldById = new Map(meal.foods.map(f => [f.fdc_id, f]));
    const newById = new Map(aiResult.foods.map(f => [f.fdc_id, f]));
    const all: { food: MealFood; status: "added" | "removed" | "changed" | "same"; oldQty?: number }[] = [];
    for (const f of meal.foods) {
      const newF = newById.get(f.fdc_id);
      if (!newF) all.push({ food: f, status: "removed" });
      else if (Math.round(newF.quantity_g) !== Math.round(f.quantity_g))
        all.push({ food: newF, status: "changed", oldQty: f.quantity_g });
      else all.push({ food: newF, status: "same" });
    }
    for (const f of aiResult.foods) {
      if (!oldById.has(f.fdc_id)) all.push({ food: f, status: "added" });
    }
    return all;
  })() : [];

  const diffInstructions = aiResult ? (() => {
    const oldSet = new Set(meal.instructions);
    const newSet = new Set(aiResult.instructions);
    return {
      removed: meal.instructions.filter(i => !newSet.has(i)),
      added: aiResult.instructions.filter(i => !oldSet.has(i)),
      unchanged: aiResult.instructions.filter(i => oldSet.has(i)),
    };
  })() : null;

  return (
    <>
      <ScrollView className="flex-1 bg-white">
        <View className="px-4 py-4">
          {/* Header */}
          <View className="mb-3">
            {editing ? (
              <TextInput
                className="text-2xl font-bold text-gray-900 border-b border-primary-400 pb-1 mb-1"
                value={editName}
                onChangeText={setEditName}
              />
            ) : (
              <Text className="text-2xl font-bold text-gray-900">{meal.name}</Text>
            )}
            <Text className="text-gray-500 capitalize mt-0.5">{meal.meal_type} · {meal.scheduled_date}</Text>
          </View>

          {/* Action row — only shown when not editing */}
          {!editing && !aiResult && (
            <View className="flex-row gap-2 mb-4">
              <Pressable
                onPress={() => setShowAIEdit(true)}
                className="flex-row items-center gap-1.5 bg-primary-50 border border-primary-200 px-4 py-2 rounded-xl"
              >
                <Ionicons name="sparkles-outline" size={15} color="#16a34a" />
                <Text className="text-sm font-semibold text-primary-700">Edit with AI</Text>
              </Pressable>
              <Pressable
                onPress={enterEditMode}
                className="flex-row items-center gap-1.5 bg-gray-100 px-4 py-2 rounded-xl"
              >
                <Ionicons name="pencil-outline" size={15} color="#6b7280" />
                <Text className="text-sm font-semibold text-gray-600">Edit manually</Text>
              </Pressable>
            </View>
          )}

          {/* AI edit diff view */}
          {aiResult && (() => {
            const oldN = meal.total_nutrition;
            const newN = aiResult.total_nutrition;
            const calDiff = Math.round(newN.calories - oldN.calories);
            const protDiff = parseFloat((newN.protein_g - oldN.protein_g).toFixed(1));
            const carbDiff = parseFloat((newN.carbs_g - oldN.carbs_g).toFixed(1));
            const fatDiff  = parseFloat((newN.fat_g  - oldN.fat_g).toFixed(1));
            const fmtDelta = (d: number, unit: string) =>
              d === 0 ? `0${unit}` : d > 0 ? `+${d}${unit}` : `${d}${unit}`;
            const deltaColor = (d: number) =>
              d > 0 ? "text-red-600" : d < 0 ? "text-green-700" : "text-gray-400";
            // calories: more is usually "worse" for a cut — flip logic with calDiff
            const calDeltaColor = calDiff > 0 ? "text-red-600" : calDiff < 0 ? "text-green-700" : "text-gray-400";

            const calDeltaStyle = calDiff > 0 ? { color: "#dc2626" } : calDiff < 0 ? { color: "#15803d" } : { color: "#9ca3af" };
            const deltaTxtStyle = (d: number) =>
              d > 0 ? { color: "#dc2626" } : d < 0 ? { color: "#15803d" } : { color: "#9ca3af" };

            return (
              <View style={{ marginBottom: 16, borderRadius: 16, overflow: "hidden", borderWidth: 1, borderColor: "#e5e7eb", shadowColor: "#000", shadowOpacity: 0.08, shadowRadius: 10, shadowOffset: { width: 0, height: 3 } }}>
                {/* Header — dark bar */}
                <View style={{ backgroundColor: "#111827", paddingHorizontal: 16, paddingVertical: 12, flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
                    <Ionicons name="sparkles" size={15} color="#a3e635" />
                    <Text style={{ fontSize: 13, fontWeight: "700", color: "#ffffff" }}>AI Edit Preview</Text>
                  </View>
                  <Text style={{ fontSize: 11, color: "#9ca3af" }} numberOfLines={1}>{aiResult.name}</Text>
                </View>

                {/* Nutrition delta */}
                <View style={{ backgroundColor: "#f9fafb", borderBottomWidth: 1, borderBottomColor: "#e5e7eb", paddingHorizontal: 16, paddingTop: 12, paddingBottom: 12 }}>
                  <Text style={{ fontSize: 10, fontWeight: "700", color: "#6b7280", textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 8 }}>Nutrition change</Text>
                  <View style={{ flexDirection: "row", gap: 8 }}>
                    {[
                      { label: "Calories", val: `${Math.round(newN.calories)}`, delta: fmtDelta(calDiff, " kcal"), valColor: "#111827", dStyle: calDeltaStyle },
                      { label: "Protein",  val: `${newN.protein_g.toFixed(1)}g`, delta: fmtDelta(protDiff, "g"), valColor: "#1d4ed8", dStyle: deltaTxtStyle(protDiff) },
                      { label: "Carbs",    val: `${newN.carbs_g.toFixed(1)}g`, delta: fmtDelta(carbDiff, "g"), valColor: "#ea580c", dStyle: deltaTxtStyle(carbDiff) },
                      { label: "Fat",      val: `${newN.fat_g.toFixed(1)}g`, delta: fmtDelta(fatDiff, "g"), valColor: "#ca8a04", dStyle: deltaTxtStyle(fatDiff) },
                    ].map((m) => (
                      <View key={m.label} style={{ flex: 1, backgroundColor: "#ffffff", borderWidth: 1, borderColor: "#e5e7eb", borderRadius: 10, padding: 8, alignItems: "center" }}>
                        <Text style={{ fontSize: 10, color: "#9ca3af", marginBottom: 2 }}>{m.label}</Text>
                        <Text style={{ fontSize: 15, fontWeight: "800", color: m.valColor }}>{m.val}</Text>
                        <Text style={[{ fontSize: 11, fontWeight: "700" }, m.dStyle]}>{m.delta}</Text>
                      </View>
                    ))}
                  </View>
                </View>

                {/* Ingredient diff */}
                <View style={{ backgroundColor: "#ffffff" }}>
                  <Text style={{ fontSize: 10, fontWeight: "700", color: "#6b7280", textTransform: "uppercase", letterSpacing: 0.8, paddingHorizontal: 16, paddingTop: 12, paddingBottom: 6 }}>Ingredients</Text>
                  {diffFoods.map((item, i) => {
                    if (item.status === "removed") {
                      return (
                        <View key={i} style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginHorizontal: 12, marginBottom: 4, borderRadius: 8, backgroundColor: "#fef2f2", borderLeftWidth: 4, borderLeftColor: "#ef4444", paddingHorizontal: 10, paddingVertical: 10 }}>
                          <View style={{ flexDirection: "row", alignItems: "center", gap: 8, flex: 1 }}>
                            <View style={{ width: 20, height: 20, borderRadius: 10, backgroundColor: "#ef4444", alignItems: "center", justifyContent: "center" }}>
                              <Text style={{ color: "#fff", fontSize: 13, fontWeight: "800", lineHeight: 20 }}>−</Text>
                            </View>
                            <Text style={{ fontSize: 13, fontWeight: "600", color: "#b91c1c", textDecorationLine: "line-through", flex: 1 }}>{item.food.description}</Text>
                          </View>
                          <Text style={{ fontSize: 13, color: "#ef4444", marginLeft: 8 }}>{Math.round(item.food.quantity_g)}g</Text>
                        </View>
                      );
                    }
                    if (item.status === "added") {
                      return (
                        <View key={i} style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginHorizontal: 12, marginBottom: 4, borderRadius: 8, backgroundColor: "#f0fdf4", borderLeftWidth: 4, borderLeftColor: "#22c55e", paddingHorizontal: 10, paddingVertical: 10 }}>
                          <View style={{ flexDirection: "row", alignItems: "center", gap: 8, flex: 1 }}>
                            <View style={{ width: 20, height: 20, borderRadius: 10, backgroundColor: "#22c55e", alignItems: "center", justifyContent: "center" }}>
                              <Text style={{ color: "#fff", fontSize: 13, fontWeight: "800", lineHeight: 20 }}>+</Text>
                            </View>
                            <Text style={{ fontSize: 13, fontWeight: "700", color: "#15803d", flex: 1 }}>{item.food.description}</Text>
                          </View>
                          <Text style={{ fontSize: 13, fontWeight: "600", color: "#16a34a", marginLeft: 8 }}>{Math.round(item.food.quantity_g)}g</Text>
                        </View>
                      );
                    }
                    if (item.status === "changed") {
                      return (
                        <View key={i} style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginHorizontal: 12, marginBottom: 4, borderRadius: 8, backgroundColor: "#fffbeb", borderLeftWidth: 4, borderLeftColor: "#f59e0b", paddingHorizontal: 10, paddingVertical: 10 }}>
                          <View style={{ flexDirection: "row", alignItems: "center", gap: 8, flex: 1 }}>
                            <View style={{ width: 20, height: 20, borderRadius: 10, backgroundColor: "#f59e0b", alignItems: "center", justifyContent: "center" }}>
                              <Text style={{ color: "#fff", fontSize: 11, fontWeight: "800", lineHeight: 20 }}>~</Text>
                            </View>
                            <Text style={{ fontSize: 13, fontWeight: "600", color: "#92400e", flex: 1 }}>{item.food.description}</Text>
                          </View>
                          <Text style={{ fontSize: 13, fontWeight: "700", color: "#b45309", marginLeft: 8 }}>
                            {item.oldQty !== undefined ? `${Math.round(item.oldQty)}g → ` : ""}{Math.round(item.food.quantity_g)}g
                          </Text>
                        </View>
                      );
                    }
                    return (
                      <View key={i} style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginHorizontal: 12, paddingHorizontal: 10, paddingVertical: 7 }}>
                        <Text style={{ fontSize: 13, color: "#9ca3af", flex: 1 }}>{item.food.description}</Text>
                        <Text style={{ fontSize: 13, color: "#d1d5db", marginLeft: 8 }}>{Math.round(item.food.quantity_g)}g</Text>
                      </View>
                    );
                  })}
                  <View style={{ height: 12 }} />
                </View>

                {/* Instruction diff */}
                {diffInstructions && (diffInstructions.removed.length > 0 || diffInstructions.added.length > 0) && (
                  <View style={{ borderTopWidth: 1, borderTopColor: "#e5e7eb", backgroundColor: "#f9fafb", paddingHorizontal: 16, paddingTop: 12, paddingBottom: 12 }}>
                    <Text style={{ fontSize: 10, fontWeight: "700", color: "#6b7280", textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 8 }}>Instructions updated</Text>
                    {diffInstructions.removed.map((step, i) => (
                      <View key={`r${i}`} style={{ marginBottom: 6, backgroundColor: "#fef2f2", borderLeftWidth: 4, borderLeftColor: "#f87171", borderRadius: 6, paddingLeft: 10, paddingRight: 8, paddingVertical: 8 }}>
                        <Text style={{ fontSize: 12, color: "#dc2626", textDecorationLine: "line-through", lineHeight: 18 }}>{step}</Text>
                      </View>
                    ))}
                    {diffInstructions.added.map((step, i) => (
                      <View key={`a${i}`} style={{ marginBottom: 6, backgroundColor: "#f0fdf4", borderLeftWidth: 4, borderLeftColor: "#22c55e", borderRadius: 6, paddingLeft: 10, paddingRight: 8, paddingVertical: 8, flexDirection: "row" }}>
                        <Text style={{ fontSize: 12, fontWeight: "700", color: "#16a34a", marginRight: 4 }}>+</Text>
                        <Text style={{ fontSize: 12, color: "#15803d", lineHeight: 18, flex: 1 }}>{step}</Text>
                      </View>
                    ))}
                  </View>
                )}

                {/* Apply/Discard */}
                <View className="flex-row gap-3 px-4 pb-4 pt-3 bg-white border-t border-gray-100">
                  <Pressable onPress={discardAIResult} className="flex-1 border border-gray-300 rounded-xl py-3 items-center">
                    <Text className="text-sm font-semibold text-gray-600">Discard</Text>
                  </Pressable>
                  <Pressable onPress={applyAIResult} className="flex-1 bg-primary-600 rounded-xl py-3 items-center">
                    <Text className="text-sm font-semibold text-white">Apply Changes</Text>
                  </Pressable>
                </View>
              </View>
            );
          })()}

          {/* Calorie highlight */}
          <View className="bg-primary-50 rounded-xl p-4 mb-4">
            <Text className="text-4xl font-bold text-primary-700">{cal}</Text>
            <Text className="text-primary-600 text-sm mt-0.5">calories</Text>
            <View className="mt-3"><MacroBar nutrients={meal.total_nutrition} /></View>
          </View>

          {/* Ingredients */}
          <Text className="text-lg font-semibold text-gray-900 mb-3">Ingredients</Text>
          <View className="bg-gray-50 rounded-xl p-4 mb-4">
            {(editing ? editFoods : meal.foods).map((food, i) => {
              const foods = editing ? editFoods : meal.foods;
              return (
                <View
                  key={food.id}
                  className={`flex-row justify-between py-2 ${i < foods.length - 1 ? "border-b border-gray-200" : ""}`}
                >
                  <View className="flex-1 mr-2">
                    <Text className="text-sm font-medium text-gray-800">{food.description}</Text>
                    {food.serving_description && (
                      <Text className="text-xs text-gray-400">{food.serving_description}</Text>
                    )}
                  </View>
                  <View className="items-end flex-row gap-2">
                    {editing ? (
                      <>
                        <View className="flex-row items-center border border-gray-300 rounded-lg overflow-hidden">
                          <TextInput
                            className="text-sm text-gray-700 px-2 py-1 w-16 text-right"
                            keyboardType="numeric"
                            value={editQuantities[food.id] ?? String(Math.round(food.quantity_g))}
                            onChangeText={(v) => setEditQuantities((prev) => ({ ...prev, [food.id]: v }))}
                          />
                          <Text className="text-xs text-gray-400 pr-2">g</Text>
                        </View>
                        <Pressable onPress={() => removeEditFood(food.id)} hitSlop={8} className="p-1">
                          <Ionicons name="close-circle" size={18} color="#f87171" />
                        </Pressable>
                      </>
                    ) : (
                      <View className="items-end">
                        <Text className="text-sm text-gray-600">{Math.round(food.quantity_g)}g</Text>
                        <Text className="text-xs text-gray-400">{Math.round(food.nutrients.calories)} kcal</Text>
                      </View>
                    )}
                  </View>
                </View>
              );
            })}
            {editing && (
              <Pressable onPress={() => setShowFoodSearch(true)} className="flex-row items-center gap-2 mt-3 py-2">
                <Ionicons name="add-circle-outline" size={20} color="#16a34a" />
                <Text className="text-sm text-primary-700 font-medium">Add ingredient</Text>
              </Pressable>
            )}
          </View>

          {/* Instructions */}
          {editing ? (
            <>
              <Text className="text-lg font-semibold text-gray-900 mb-3">Instructions</Text>
              <View className="mb-4">
                {editInstructions.map((step, i) => (
                  <View key={i} className="flex-row mb-2 items-start gap-2">
                    <View className="w-7 h-7 rounded-full bg-primary-100 items-center justify-center flex-shrink-0 mt-1">
                      <Text className="text-primary-700 text-xs font-bold">{i + 1}</Text>
                    </View>
                    <TextInput
                      className="flex-1 border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-700 bg-white"
                      multiline
                      value={step}
                      onChangeText={(v) => setEditInstructions((prev) => prev.map((s, idx) => idx === i ? v : s))}
                    />
                    <Pressable onPress={() => setEditInstructions((prev) => prev.filter((_, idx) => idx !== i))} hitSlop={8} className="p-1 mt-1">
                      <Ionicons name="close-circle" size={18} color="#f87171" />
                    </Pressable>
                  </View>
                ))}
                <Pressable onPress={() => setEditInstructions((prev) => [...prev, ""])} className="flex-row items-center gap-2 mt-2">
                  <Ionicons name="add-circle-outline" size={20} color="#16a34a" />
                  <Text className="text-sm text-primary-700 font-medium">Add step</Text>
                </Pressable>
              </View>
            </>
          ) : (
            meal.instructions.length > 0 && (
              <>
                <Text className="text-lg font-semibold text-gray-900 mb-3">Instructions</Text>
                <View className="mb-4">
                  {meal.instructions.map((step, i) => (
                    <View key={i} className="flex-row mb-3">
                      <View className="w-7 h-7 rounded-full bg-primary-100 items-center justify-center mr-3 flex-shrink-0 mt-0.5">
                        <Text className="text-primary-700 text-xs font-bold">{i + 1}</Text>
                      </View>
                      <Text className="text-sm text-gray-700 flex-1 leading-relaxed">{step}</Text>
                    </View>
                  ))}
                </View>
              </>
            )
          )}

          {/* Edit save/cancel */}
          {editing && (
            <View className="flex-row gap-3 mb-4">
              <Pressable onPress={cancelEdit} className="flex-1 border border-gray-300 rounded-xl py-3 items-center">
                <Text className="text-sm font-semibold text-gray-700">Cancel</Text>
              </Pressable>
              <Pressable onPress={saveEdits} disabled={saving} className="flex-1 bg-primary-600 rounded-xl py-3 items-center">
                {saving ? <ActivityIndicator size="small" color="white" /> : <Text className="text-sm font-semibold text-white">Save Changes</Text>}
              </Pressable>
            </View>
          )}

          {/* Nutrition toggle */}
          {!editing && !aiResult && (
            <>
              <Pressable
                onPress={() => setShowNutrition((v) => !v)}
                className="flex-row items-center justify-between py-3 border-t border-gray-100 mb-4"
              >
                <Text className="text-base font-semibold text-gray-800">Full Nutrition Facts</Text>
                <Ionicons name={showNutrition ? "chevron-up" : "chevron-down"} size={20} color="#6b7280" />
              </Pressable>
              {showNutrition && <NutritionLabel nutrients={meal.total_nutrition} title={meal.name} />}
              {meal.notes && (
                <View className="mt-4 bg-amber-50 rounded-xl p-4">
                  <Text className="text-sm text-amber-800">{meal.notes}</Text>
                </View>
              )}
            </>
          )}
        </View>
      </ScrollView>

      <FoodSearchModal visible={showFoodSearch} onClose={() => setShowFoodSearch(false)} onSelect={addEditFood} />

      {/* AI edit input modal */}
      <Modal visible={showAIEdit} transparent animationType="slide" onRequestClose={() => setShowAIEdit(false)}>
        <View className="flex-1 justify-end bg-black/40">
          <View className="bg-white rounded-t-3xl px-4 pt-4 pb-8">
            <View className="items-center mb-3"><View className="w-10 h-1 rounded-full bg-gray-200" /></View>
            <View className="flex-row items-center gap-2 mb-2">
              <Ionicons name="sparkles-outline" size={20} color="#16a34a" />
              <Text className="text-base font-bold text-gray-900">Edit with AI</Text>
            </View>
            <Text className="text-sm text-gray-500 mb-3">
              Describe what to change — e.g. "replace chicken with salmon" or "double the protein, keep carbs similar"
            </Text>
            <TextInput
              className="border border-gray-200 rounded-xl px-4 py-3 text-sm text-gray-900 bg-gray-50 min-h-[80px]"
              placeholder="What would you like to change?"
              placeholderTextColor="#9ca3af"
              multiline
              value={aiInstruction}
              onChangeText={setAIInstruction}
              editable={!aiLoading}
            />
            <View className="flex-row gap-3 mt-4">
              <Pressable onPress={() => { setShowAIEdit(false); setAIInstruction(""); }} className="flex-1 border border-gray-300 rounded-xl py-3 items-center" disabled={aiLoading}>
                <Text className="text-sm font-semibold text-gray-700">Cancel</Text>
              </Pressable>
              <Pressable
                onPress={handleAIEdit}
                disabled={aiLoading || !aiInstruction.trim()}
                className={`flex-1 rounded-xl py-3 items-center ${aiLoading || !aiInstruction.trim() ? "bg-gray-200" : "bg-primary-600"}`}
              >
                {aiLoading ? (
                  <View className="flex-row items-center gap-2">
                    <ActivityIndicator size="small" color="#16a34a" />
                    <Text className="text-sm font-semibold text-gray-600">Editing…</Text>
                  </View>
                ) : (
                  <Text className="text-sm font-semibold text-white">Apply Edit</Text>
                )}
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </>
  );
}
