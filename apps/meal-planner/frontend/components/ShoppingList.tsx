import { View, Text, ScrollView } from "react-native";
import { MealPlan, ShoppingItem } from "../lib/types";

function buildShoppingList(plan: MealPlan): ShoppingItem[] {
  const map = new Map<number, ShoppingItem>();
  for (const meal of plan.meals) {
    for (const food of meal.foods) {
      const existing = map.get(food.fdc_id);
      if (existing) {
        existing.total_g += food.quantity_g;
        if (!existing.meals.includes(meal.name)) {
          existing.meals.push(meal.name);
        }
      } else {
        map.set(food.fdc_id, {
          description: food.description,
          fdc_id: food.fdc_id,
          total_g: food.quantity_g,
          meals: [meal.name],
        });
      }
    }
  }
  return Array.from(map.values()).sort((a, b) =>
    a.description.localeCompare(b.description)
  );
}

function formatQuantity(g: number): string {
  if (g >= 1000) return `${(g / 1000).toFixed(2)} kg`;
  return `${Math.round(g)} g`;
}

interface Props {
  plan: MealPlan;
}

export default function ShoppingList({ plan }: Props) {
  const items = buildShoppingList(plan);

  return (
    <ScrollView className="flex-1">
      <Text className="text-xl font-bold text-gray-900 mb-1">{plan.name}</Text>
      <Text className="text-sm text-gray-500 mb-4">{items.length} ingredients</Text>

      {items.map((item) => (
        <View
          key={item.fdc_id}
          className="flex-row items-start justify-between py-3 border-b border-gray-100"
        >
          <View className="flex-1 mr-4">
            <Text className="text-sm font-medium text-gray-900">{item.description}</Text>
            <Text className="text-xs text-gray-400 mt-0.5">
              Used in: {item.meals.join(", ")}
            </Text>
          </View>
          <Text className="text-sm font-semibold text-gray-700 whitespace-nowrap">
            {formatQuantity(item.total_g)}
          </Text>
        </View>
      ))}
    </ScrollView>
  );
}
