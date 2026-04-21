import { View, Text, Pressable } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { Meal } from "../lib/types";

const MEAL_TYPE_COLOR: Record<string, string> = {
  breakfast: "bg-amber-100 text-amber-800",
  lunch: "bg-sky-100 text-sky-800",
  dinner: "bg-violet-100 text-violet-800",
  snack: "bg-green-100 text-green-800",
};

interface Props {
  meal: Meal;
  onPress?: () => void;
  isFavorited?: boolean;
  favoriteId?: string;
  onFavorite?: () => void;
  onUnfavorite?: () => void;
}

export default function MealCard({
  meal,
  onPress,
  isFavorited,
  onFavorite,
  onUnfavorite,
}: Props) {
  const badgeClass = MEAL_TYPE_COLOR[meal.meal_type] ?? "bg-gray-100 text-gray-800";
  const cal = Math.round(meal.total_nutrition.calories);
  const protein = meal.total_nutrition.protein_g.toFixed(1);
  const carbs = meal.total_nutrition.carbs_g.toFixed(1);
  const fat = meal.total_nutrition.fat_g.toFixed(1);

  const showHeart = onFavorite !== undefined || onUnfavorite !== undefined;

  return (
    <Pressable
      onPress={onPress}
      className="bg-white rounded-xl p-4 mb-3 shadow-sm border border-gray-100 active:opacity-70 web:hover:shadow-md web:cursor-pointer"
    >
      <View className="flex-row items-start justify-between mb-2">
        <Text className="text-base font-semibold text-gray-900 flex-1 mr-2">{meal.name}</Text>
        <View className="flex-row items-center gap-2">
          <View className={`px-2 py-0.5 rounded-full ${badgeClass.split(" ")[0]}`}>
            <Text className={`text-xs font-medium ${badgeClass.split(" ")[1]}`}>
              {meal.meal_type.charAt(0).toUpperCase() + meal.meal_type.slice(1)}
            </Text>
          </View>
          {showHeart && (
            <Pressable
              onPress={(e) => {
                e.stopPropagation?.();
                if (isFavorited) onUnfavorite?.();
                else onFavorite?.();
              }}
              hitSlop={8}
              className="p-1 web:cursor-pointer"
            >
              <Ionicons
                name={isFavorited ? "heart" : "heart-outline"}
                size={18}
                color={isFavorited ? "#ef4444" : "#d1d5db"}
              />
            </Pressable>
          )}
        </View>
      </View>

      <View className="flex-row gap-4">
        <View className="items-center">
          <Text className="text-lg font-bold text-gray-900">{cal}</Text>
          <Text className="text-xs text-gray-500">kcal</Text>
        </View>
        <View className="items-center">
          <Text className="text-base font-semibold text-blue-600">{protein}g</Text>
          <Text className="text-xs text-gray-500">protein</Text>
        </View>
        <View className="items-center">
          <Text className="text-base font-semibold text-orange-500">{carbs}g</Text>
          <Text className="text-xs text-gray-500">carbs</Text>
        </View>
        <View className="items-center">
          <Text className="text-base font-semibold text-yellow-500">{fat}g</Text>
          <Text className="text-xs text-gray-500">fat</Text>
        </View>
      </View>

      {meal.foods.length > 0 && (
        <Text className="text-xs text-gray-400 mt-2">
          {meal.foods.map((f) => f.description).slice(0, 4).join(", ")}
          {meal.foods.length > 4 ? ` +${meal.foods.length - 4} more` : ""}
        </Text>
      )}
    </Pressable>
  );
}
