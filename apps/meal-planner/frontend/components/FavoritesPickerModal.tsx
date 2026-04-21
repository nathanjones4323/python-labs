import { View, Text, Pressable, ScrollView, Modal } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { FavoriteMealSummary } from "../lib/types";

const MEAL_TYPE_COLOR: Record<string, string> = {
  breakfast: "bg-amber-100 text-amber-800",
  lunch: "bg-sky-100 text-sky-800",
  dinner: "bg-violet-100 text-violet-800",
  snack: "bg-green-100 text-green-800",
};

interface Props {
  visible: boolean;
  onClose: () => void;
  favorites: FavoriteMealSummary[];
  multiSelect?: boolean;
  selectedIds?: Set<string>;
  onToggle?: (id: string) => void;
  onSelect?: (fav: FavoriteMealSummary) => void;
  title?: string;
  subtitle?: string;
}

export default function FavoritesPickerModal({
  visible,
  onClose,
  favorites,
  multiSelect = false,
  selectedIds,
  onToggle,
  onSelect,
  title = "Add Favorites",
  subtitle = "Select meals to include in your plan",
}: Props) {
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <View className="flex-1 justify-end bg-black/40">
        <View className="bg-white rounded-t-3xl" style={{ maxHeight: "75%" }}>
          <View className="items-center pt-3 pb-1">
            <View className="w-10 h-1 rounded-full bg-gray-200" />
          </View>

          <View className="flex-row items-center justify-between px-4 py-3 border-b border-gray-100">
            <View className="flex-row items-center gap-2">
              <Ionicons name="heart" size={18} color="#ef4444" />
              <Text className="text-base font-bold text-gray-900">{title}</Text>
            </View>
            <Pressable onPress={onClose} className="p-1 web:cursor-pointer">
              <Ionicons name="close" size={22} color="#6b7280" />
            </Pressable>
          </View>

          <Text className="text-sm text-gray-500 px-4 pt-2 pb-1">{subtitle}</Text>

          {favorites.length === 0 ? (
            <View className="py-12 items-center px-6">
              <Ionicons name="heart-outline" size={32} color="#d1d5db" />
              <Text className="text-gray-400 text-sm text-center mt-3">
                No favorites yet. Heart meals from your plans to save them here.
              </Text>
            </View>
          ) : (
            <ScrollView className="flex-1 px-4 py-2">
              {favorites.map((fav) => {
                const badgeClass = MEAL_TYPE_COLOR[fav.meal_type] ?? "bg-gray-100 text-gray-800";
                const cal = Math.round(fav.total_nutrition.calories);
                const protein = fav.total_nutrition.protein_g.toFixed(1);
                const isSelected = multiSelect ? selectedIds?.has(fav.id) : false;

                return (
                  <Pressable
                    key={fav.id}
                    onPress={() => {
                      if (multiSelect) onToggle?.(fav.id);
                      else { onSelect?.(fav); onClose(); }
                    }}
                    className={`flex-row items-center py-3 border-b border-gray-50 active:bg-gray-50 web:cursor-pointer web:hover:bg-gray-50 rounded-xl ${isSelected ? "bg-primary-50" : ""}`}
                  >
                    <View className="flex-1 mr-3">
                      <View className="flex-row items-center gap-2 mb-1">
                        <Text className="text-sm font-semibold text-gray-900 flex-1" numberOfLines={1}>
                          {fav.name}
                        </Text>
                        <View className={`px-2 py-0.5 rounded-full ${badgeClass.split(" ")[0]}`}>
                          <Text className={`text-xs font-medium ${badgeClass.split(" ")[1]}`}>
                            {fav.meal_type.charAt(0).toUpperCase() + fav.meal_type.slice(1)}
                          </Text>
                        </View>
                      </View>
                      <Text className="text-xs text-gray-500">
                        {cal} kcal · {protein}g protein
                      </Text>
                    </View>

                    {multiSelect ? (
                      <View
                        className={`w-6 h-6 rounded-full border-2 items-center justify-center ${
                          isSelected ? "bg-primary-600 border-primary-600" : "border-gray-300"
                        }`}
                      >
                        {isSelected && <Ionicons name="checkmark" size={14} color="white" />}
                      </View>
                    ) : (
                      <Ionicons name="chevron-forward" size={16} color="#d1d5db" />
                    )}
                  </Pressable>
                );
              })}
            </ScrollView>
          )}

          {multiSelect && (
            <View className="px-4 pb-6 pt-3 border-t border-gray-100">
              <Pressable
                onPress={onClose}
                className="bg-primary-600 rounded-xl py-3 items-center web:cursor-pointer web:hover:opacity-90"
              >
                <Text className="text-white font-semibold text-sm">
                  Done{selectedIds && selectedIds.size > 0 ? ` (${selectedIds.size} selected)` : ""}
                </Text>
              </Pressable>
            </View>
          )}
        </View>
      </View>
    </Modal>
  );
}
