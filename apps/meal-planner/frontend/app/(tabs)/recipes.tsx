import { useState, useCallback } from "react";
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
import { listFavorites, unfavoriteMeal } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
import { FavoriteMeal } from "../../lib/types";
import MacroBar from "../../components/MacroBar";
import NutritionLabel from "../../components/NutritionLabel";

const MEAL_TYPE_COLOR: Record<string, string> = {
  breakfast: "bg-amber-100 text-amber-800",
  lunch: "bg-sky-100 text-sky-800",
  dinner: "bg-violet-100 text-violet-800",
  snack: "bg-green-100 text-green-800",
};

const FILTER_OPTIONS = ["all", "breakfast", "lunch", "dinner", "snack"] as const;
type Filter = (typeof FILTER_OPTIONS)[number];

export default function RecipesScreen() {
  const router = useRouter();
  const [userId, setUserId] = useState<string | null>(null);
  const [favorites, setFavorites] = useState<FavoriteMeal[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [unfavoriting, setUnfavoriting] = useState<string | null>(null);

  useFocusEffect(
    useCallback(() => {
      loadUserId().then((uid) => {
        setUserId(uid);
        if (uid) loadFavorites(uid);
        else setLoading(false);
      });
    }, [])
  );

  async function loadFavorites(uid: string) {
    try {
      const data = await listFavorites(uid);
      setFavorites(data);
    } catch {
      // silently fail — user just sees empty state
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  async function handleUnfavorite(fav: FavoriteMeal) {
    setUnfavoriting(fav.id);
    try {
      await unfavoriteMeal(fav.id);
      setFavorites((prev) => prev.filter((f) => f.id !== fav.id));
      if (expandedId === fav.id) setExpandedId(null);
    } catch {
      // silently fail
    } finally {
      setUnfavoriting(null);
    }
  }

  const filtered = filter === "all" ? favorites : favorites.filter((f) => f.meal_type === filter);

  if (loading) {
    return (
      <View className="flex-1 items-center justify-center bg-white">
        <ActivityIndicator color="#16a34a" />
      </View>
    );
  }

  if (!userId || favorites.length === 0) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8">
        <View className="w-16 h-16 rounded-3xl bg-red-50 items-center justify-center mb-4">
          <Ionicons name="heart-outline" size={28} color="#ef4444" />
        </View>
        <Text className="text-xl font-bold text-gray-800 mb-2">No saved recipes yet</Text>
        <Text className="text-gray-500 text-center mb-6 leading-5">
          Heart any meal from your plans to save it here, or create your own recipe.
        </Text>
        <Pressable
          onPress={() => router.push("/create-recipe")}
          className="bg-primary-600 px-6 py-3 rounded-xl mb-3 web:cursor-pointer web:hover:opacity-90"
        >
          <Text className="text-white font-semibold">Create a Recipe</Text>
        </Pressable>
        <Pressable
          onPress={() => router.push("/(tabs)/plans")}
          className="px-6 py-3 rounded-xl web:cursor-pointer web:hover:opacity-90"
        >
          <Text className="text-gray-500 font-semibold">Go to Meal Plans</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <ScrollView
      className="flex-1 bg-white"
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => {
            setRefreshing(true);
            if (userId) loadFavorites(userId);
          }}
        />
      }
    >
      {/* Filter chips + create button */}
      <View className="flex-row items-center border-b border-gray-100">
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          className="flex-1"
          contentContainerStyle={{ paddingHorizontal: 16, paddingVertical: 10, gap: 8 }}
        >
          {FILTER_OPTIONS.map((opt) => {
            const active = filter === opt;
            return (
              <Pressable
                key={opt}
                onPress={() => setFilter(opt)}
                className={`px-4 py-1.5 rounded-full border web:cursor-pointer ${
                  active
                    ? "bg-primary-600 border-primary-600"
                    : "bg-white border-gray-200 web:hover:border-primary-300"
                }`}
              >
                <Text
                  className={`text-xs font-semibold ${active ? "text-white" : "text-gray-600"}`}
                >
                  {opt.charAt(0).toUpperCase() + opt.slice(1)}
                </Text>
              </Pressable>
            );
          })}
        </ScrollView>
        <Pressable
          onPress={() => router.push("/create-recipe")}
          className="flex-row items-center gap-1 px-3 py-2 mr-2 rounded-full bg-primary-50 web:cursor-pointer web:hover:bg-primary-100"
        >
          <Ionicons name="add" size={16} color="#16a34a" />
          <Text className="text-xs font-semibold text-primary-700">New</Text>
        </Pressable>
      </View>

      {/* Recipe count */}
      <View className="px-4 pt-3 pb-1">
        <Text className="text-xs text-gray-400 font-medium uppercase tracking-wider">
          {filtered.length} {filtered.length === 1 ? "recipe" : "recipes"}
        </Text>
      </View>

      {/* Recipe cards */}
      <View className="px-4 pb-8 web:grid web:grid-cols-2 web:gap-4">
        {filtered.map((fav) => {
          const isExpanded = expandedId === fav.id;
          const badgeClass = MEAL_TYPE_COLOR[fav.meal_type] ?? "bg-gray-100 text-gray-800";
          const cal = Math.round(fav.total_nutrition.calories);
          const protein = fav.total_nutrition.protein_g.toFixed(1);

          return (
            <View
              key={fav.id}
              className="mb-3 rounded-2xl border border-gray-100 overflow-hidden bg-white shadow-sm web:hover:shadow-md"
              style={{ shadowColor: "#000", shadowOpacity: 0.04, shadowRadius: 4, shadowOffset: { width: 0, height: 2 } }}
            >
              {/* Card header — tap to expand */}
              <Pressable
                onPress={() => setExpandedId(isExpanded ? null : fav.id)}
                className="p-4 active:bg-gray-50 web:cursor-pointer"
              >
                <View className="flex-row items-start justify-between mb-2">
                  <Text className="text-base font-semibold text-gray-900 flex-1 mr-2" numberOfLines={2}>
                    {fav.name}
                  </Text>
                  <View className="flex-row items-center gap-2 flex-shrink-0">
                    <View className={`px-2 py-0.5 rounded-full ${badgeClass.split(" ")[0]}`}>
                      <Text className={`text-xs font-medium ${badgeClass.split(" ")[1]}`}>
                        {fav.meal_type.charAt(0).toUpperCase() + fav.meal_type.slice(1)}
                      </Text>
                    </View>
                    <Ionicons
                      name={isExpanded ? "chevron-up" : "chevron-down"}
                      size={16}
                      color="#9ca3af"
                    />
                  </View>
                </View>

                <View className="flex-row items-center gap-4 mb-2">
                  <Text className="text-sm font-bold text-gray-900">{cal} kcal</Text>
                  <Text className="text-sm text-blue-600">{protein}g protein</Text>
                </View>

                <MacroBar nutrients={fav.total_nutrition} />
              </Pressable>

              {/* Expanded detail */}
              {isExpanded && (
                <View className="border-t border-gray-100">
                  {/* Ingredients */}
                  {fav.foods.length > 0 && (
                    <View className="px-4 pt-3 pb-2">
                      <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                        Ingredients
                      </Text>
                      {fav.foods.map((food, i) => (
                        <View
                          key={food.id}
                          className={`flex-row justify-between py-1.5 ${
                            i < fav.foods.length - 1 ? "border-b border-gray-50" : ""
                          }`}
                        >
                          <Text className="text-sm text-gray-700 flex-1 mr-2" numberOfLines={2}>
                            {food.description}
                          </Text>
                          <Text className="text-sm text-gray-500 flex-shrink-0">
                            {Math.round(food.quantity_g)}g
                          </Text>
                        </View>
                      ))}
                    </View>
                  )}

                  {/* Instructions */}
                  {fav.instructions.length > 0 && (
                    <View className="px-4 pt-2 pb-3 border-t border-gray-50">
                      <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
                        Instructions
                      </Text>
                      {fav.instructions.map((step, i) => (
                        <View key={i} className="flex-row mb-2">
                          <View className="w-6 h-6 rounded-full bg-primary-100 items-center justify-center mr-2 flex-shrink-0 mt-0.5">
                            <Text className="text-primary-700 text-xs font-bold">{i + 1}</Text>
                          </View>
                          <Text className="text-sm text-gray-700 flex-1 leading-relaxed">{step}</Text>
                        </View>
                      ))}
                    </View>
                  )}

                  {/* Full nutrition label */}
                  <View className="px-4 pb-3 border-t border-gray-50">
                    <NutritionLabel nutrients={fav.total_nutrition} title="Nutrition Facts" />
                  </View>

                  {/* Unfavorite button */}
                  <View className="px-4 pb-4 border-t border-gray-100">
                    <Pressable
                      onPress={() => handleUnfavorite(fav)}
                      disabled={unfavoriting === fav.id}
                      className="flex-row items-center justify-center gap-2 border border-red-200 rounded-xl py-2.5 mt-3 active:bg-red-50 web:cursor-pointer web:hover:bg-red-50"
                    >
                      {unfavoriting === fav.id ? (
                        <ActivityIndicator size="small" color="#ef4444" />
                      ) : (
                        <>
                          <Ionicons name="heart-dislike-outline" size={16} color="#ef4444" />
                          <Text className="text-sm font-semibold text-red-600">Remove from Recipes</Text>
                        </>
                      )}
                    </Pressable>
                  </View>
                </View>
              )}
            </View>
          );
        })}
      </View>
    </ScrollView>
  );
}
