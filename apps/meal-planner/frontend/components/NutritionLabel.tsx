import { View, Text } from "react-native";
import { NutrientSummary } from "../lib/types";

interface Props {
  nutrients: NutrientSummary;
  title?: string;
}

function Row({ label, value, unit, bold }: { label: string; value: number; unit: string; bold?: boolean }) {
  return (
    <View className={`flex-row justify-between py-1 ${bold ? "border-t-2 border-gray-800" : "border-t border-gray-200"}`}>
      <Text className={`text-sm text-gray-800 ${bold ? "font-bold" : ""}`}>{label}</Text>
      <Text className={`text-sm text-gray-800 ${bold ? "font-bold" : ""}`}>
        {value > 0 ? `${value % 1 === 0 ? value : value.toFixed(1)}${unit}` : "—"}
      </Text>
    </View>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View className="mt-3">
      <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">{title}</Text>
      {children}
    </View>
  );
}

export default function NutritionLabel({ nutrients, title = "Nutrition Facts" }: Props) {
  return (
    <View className="bg-white rounded-xl p-4 border border-gray-200">
      <Text className="text-lg font-bold text-gray-900 mb-2">{title}</Text>

      <Section title="Macronutrients">
        <Row label="Calories" value={Math.round(nutrients.calories)} unit=" kcal" bold />
        <Row label="Protein" value={nutrients.protein_g} unit="g" />
        <Row label="Total Fat" value={nutrients.fat_g} unit="g" />
        <Row label="  Saturated Fat" value={nutrients.saturated_fat_g} unit="g" />
        <Row label="Carbohydrates" value={nutrients.carbs_g} unit="g" />
        <Row label="  Dietary Fiber" value={nutrients.fiber_g} unit="g" />
        <Row label="  Total Sugars" value={nutrients.sugar_g} unit="g" />
        <Row label="Cholesterol" value={nutrients.cholesterol_mg} unit="mg" />
        <Row label="Sodium" value={nutrients.sodium_mg} unit="mg" />
      </Section>

      <Section title="Vitamins">
        <Row label="Vitamin A" value={nutrients.vitamin_a_ug} unit="μg RAE" />
        <Row label="Vitamin C" value={nutrients.vitamin_c_mg} unit="mg" />
        <Row label="Vitamin D" value={nutrients.vitamin_d_ug} unit="μg" />
        <Row label="Vitamin E" value={nutrients.vitamin_e_mg} unit="mg" />
        <Row label="Vitamin K" value={nutrients.vitamin_k_ug} unit="μg" />
        <Row label="Thiamin (B1)" value={nutrients.thiamin_mg} unit="mg" />
        <Row label="Riboflavin (B2)" value={nutrients.riboflavin_mg} unit="mg" />
        <Row label="Niacin (B3)" value={nutrients.niacin_mg} unit="mg" />
        <Row label="Vitamin B6" value={nutrients.vitamin_b6_mg} unit="mg" />
        <Row label="Vitamin B12" value={nutrients.vitamin_b12_ug} unit="μg" />
        <Row label="Folate" value={nutrients.folate_ug} unit="μg" />
      </Section>

      <Section title="Minerals">
        <Row label="Calcium" value={nutrients.calcium_mg} unit="mg" />
        <Row label="Iron" value={nutrients.iron_mg} unit="mg" />
        <Row label="Magnesium" value={nutrients.magnesium_mg} unit="mg" />
        <Row label="Phosphorus" value={nutrients.phosphorus_mg} unit="mg" />
        <Row label="Potassium" value={nutrients.potassium_mg} unit="mg" />
        <Row label="Zinc" value={nutrients.zinc_mg} unit="mg" />
      </Section>

      <Text className="text-xs text-gray-400 mt-3">Source: USDA FoodData Central</Text>
    </View>
  );
}
