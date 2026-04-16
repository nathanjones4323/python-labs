import { View, Text } from "react-native";
import { NutrientSummary } from "../lib/types";

interface Props {
  nutrients: NutrientSummary;
}

export default function MacroBar({ nutrients }: Props) {
  const { protein_g, carbs_g, fat_g } = nutrients;
  const total = protein_g + carbs_g + fat_g || 1;
  const pPct = (protein_g / total) * 100;
  const cPct = (carbs_g / total) * 100;
  const fPct = (fat_g / total) * 100;

  return (
    <View>
      <View className="flex-row rounded-full overflow-hidden h-3 mb-2">
        <View style={{ flex: pPct }} className="bg-blue-500" />
        <View style={{ flex: cPct }} className="bg-orange-400" />
        <View style={{ flex: fPct }} className="bg-yellow-400" />
      </View>
      <View className="flex-row gap-4">
        <View className="flex-row items-center gap-1">
          <View className="w-2 h-2 rounded-full bg-blue-500" />
          <Text className="text-xs text-gray-600">Protein {protein_g.toFixed(1)}g</Text>
        </View>
        <View className="flex-row items-center gap-1">
          <View className="w-2 h-2 rounded-full bg-orange-400" />
          <Text className="text-xs text-gray-600">Carbs {carbs_g.toFixed(1)}g</Text>
        </View>
        <View className="flex-row items-center gap-1">
          <View className="w-2 h-2 rounded-full bg-yellow-400" />
          <Text className="text-xs text-gray-600">Fat {fat_g.toFixed(1)}g</Text>
        </View>
      </View>
    </View>
  );
}
