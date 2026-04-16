import { useEffect, useState } from "react";
import {
  View,
  Text,
  ScrollView,
  ActivityIndicator,
  Pressable,
} from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { getMealPlan } from "../../lib/api";
import { Meal, MealPlan } from "../../lib/types";
import NutritionLabel from "../../components/NutritionLabel";
import MacroBar from "../../components/MacroBar";

// This screen receives meal id; we look up the plan that contains it
// In practice, pass plan_id as well via query params for efficiency
export default function MealDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const [meal, setMeal] = useState<Meal | null>(null);
  const [loading, setLoading] = useState(true);
  const [showNutrition, setShowNutrition] = useState(false);

  // We receive planId as query param from the plans screen
  const planId = useLocalSearchParams<{ planId?: string }>().planId;

  useEffect(() => {
    if (!planId) {
      // Fallback — can't load without planId; go back
      router.back();
      return;
    }
    getMealPlan(planId)
      .then((plan: MealPlan) => {
        const found = plan.meals.find((m) => m.id === id);
        setMeal(found ?? null);
      })
      .finally(() => setLoading(false));
  }, [id, planId]);

  if (loading) {
    return (
      <View className="flex-1 items-center justify-center">
        <ActivityIndicator color="#16a34a" />
      </View>
    );
  }

  if (!meal) {
    return (
      <View className="flex-1 items-center justify-center px-8">
        <Text className="text-gray-500">Meal not found.</Text>
      </View>
    );
  }

  const cal = Math.round(meal.total_nutrition.calories);

  return (
    <ScrollView className="flex-1 bg-white">
      <View className="px-4 py-4">
        {/* Header */}
        <View className="mb-4">
          <Text className="text-2xl font-bold text-gray-900">{meal.name}</Text>
          <Text className="text-gray-500 capitalize mt-0.5">{meal.meal_type} · {meal.scheduled_date}</Text>
        </View>

        {/* Calorie highlight */}
        <View className="bg-primary-50 rounded-xl p-4 mb-4">
          <Text className="text-4xl font-bold text-primary-700">{cal}</Text>
          <Text className="text-primary-600 text-sm mt-0.5">calories</Text>
          <View className="mt-3">
            <MacroBar nutrients={meal.total_nutrition} />
          </View>
        </View>

        {/* Ingredients */}
        <Text className="text-lg font-semibold text-gray-900 mb-3">Ingredients</Text>
        <View className="bg-gray-50 rounded-xl p-4 mb-4">
          {meal.foods.map((food, i) => (
            <View
              key={food.id}
              className={`flex-row justify-between py-2 ${i < meal.foods.length - 1 ? "border-b border-gray-200" : ""}`}
            >
              <View className="flex-1 mr-2">
                <Text className="text-sm font-medium text-gray-800">{food.description}</Text>
                {food.serving_description && (
                  <Text className="text-xs text-gray-400">{food.serving_description}</Text>
                )}
              </View>
              <View className="items-end">
                <Text className="text-sm text-gray-600">{Math.round(food.quantity_g)}g</Text>
                <Text className="text-xs text-gray-400">
                  {Math.round(food.nutrients.calories)} kcal
                </Text>
              </View>
            </View>
          ))}
        </View>

        {/* Instructions */}
        {meal.instructions.length > 0 && (
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
        )}

        {/* Nutrition toggle */}
        <Pressable
          onPress={() => setShowNutrition((v) => !v)}
          className="flex-row items-center justify-between py-3 border-t border-gray-100 mb-4"
        >
          <Text className="text-base font-semibold text-gray-800">Full Nutrition Facts</Text>
          <Ionicons
            name={showNutrition ? "chevron-up" : "chevron-down"}
            size={20}
            color="#6b7280"
          />
        </Pressable>

        {showNutrition && (
          <NutritionLabel nutrients={meal.total_nutrition} title={meal.name} />
        )}

        {meal.notes && (
          <View className="mt-4 bg-amber-50 rounded-xl p-4">
            <Text className="text-sm text-amber-800">{meal.notes}</Text>
          </View>
        )}
      </View>
    </ScrollView>
  );
}
