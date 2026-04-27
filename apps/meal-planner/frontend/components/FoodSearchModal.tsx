import { useState, useEffect, useRef } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  Modal,
  ActivityIndicator,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { searchFoods } from "../lib/api";
import { FoodSearchResult } from "../lib/types";

interface Props {
  visible: boolean;
  onClose: () => void;
  onSelect: (food: FoodSearchResult & { quantity_g: number }) => void;
}

export default function FoodSearchModal({ visible, onClose, onSelect }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FoodSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<FoodSearchResult | null>(null);
  const [quantity, setQuantity] = useState("100");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!visible) {
      setQuery("");
      setResults([]);
      setSelected(null);
      setQuantity("100");
    }
  }, [visible]);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (query.trim().length < 2) {
      setResults([]);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const data = await searchFoods(query.trim(), 10);
        setResults(data.results);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 400);
  }, [query]);

  function handleAdd() {
    if (!selected) return;
    const qty = parseFloat(quantity);
    if (isNaN(qty) || qty <= 0) return;
    onSelect({ ...selected, quantity_g: qty });
    onClose();
  }

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <View className="flex-1 justify-end bg-black/40">
        <View className="bg-white rounded-t-3xl" style={{ maxHeight: "80%" }}>
          {/* Handle + header */}
          <View className="items-center pt-3 pb-1">
            <View className="w-10 h-1 rounded-full bg-gray-200" />
          </View>
          <View className="flex-row items-center justify-between px-4 py-3 border-b border-gray-100">
            <Text className="text-base font-bold text-gray-900">Add Ingredient</Text>
            <Pressable onPress={onClose} className="p-1">
              <Ionicons name="close" size={22} color="#6b7280" />
            </Pressable>
          </View>

          {/* Search input */}
          <View className="px-4 py-3 border-b border-gray-100">
            <View className="flex-row items-center bg-gray-100 rounded-xl px-3 gap-2">
              <Ionicons name="search-outline" size={18} color="#9ca3af" />
              <TextInput
                className="flex-1 py-2.5 text-sm text-gray-900"
                placeholder="Search USDA database…"
                placeholderTextColor="#9ca3af"
                value={query}
                onChangeText={setQuery}
                autoFocus
              />
              {loading && <ActivityIndicator size="small" color="#16a34a" />}
            </View>
          </View>

          {/* Results or selected food detail */}
          {selected ? (
            <View className="px-4 py-4">
              <View className="flex-row items-start mb-4">
                <View className="flex-1">
                  <Text className="text-sm font-semibold text-gray-900 leading-snug">
                    {selected.description}
                  </Text>
                  {selected.food_category && (
                    <Text className="text-xs text-gray-400 mt-0.5">{selected.food_category}</Text>
                  )}
                </View>
                <Pressable
                  onPress={() => setSelected(null)}
                  className="ml-3 p-1"
                >
                  <Ionicons name="arrow-back-outline" size={18} color="#6b7280" />
                </Pressable>
              </View>

              <View className="flex-row items-center gap-3 mb-5">
                <Text className="text-sm text-gray-700 font-medium">Quantity (g):</Text>
                <TextInput
                  className="border border-gray-300 rounded-xl px-4 py-2 text-sm text-gray-900 w-28 text-center"
                  keyboardType="numeric"
                  value={quantity}
                  onChangeText={setQuantity}
                />
              </View>

              <Pressable
                onPress={handleAdd}
                className="bg-primary-600 rounded-xl py-3 items-center"
              >
                <Text className="text-white font-semibold text-sm">Add to Meal</Text>
              </Pressable>
            </View>
          ) : (
            <ScrollView keyboardShouldPersistTaps="handled" style={{ maxHeight: 350 }}>
              {results.length === 0 && query.trim().length >= 2 && !loading ? (
                <View className="py-8 items-center">
                  <Text className="text-gray-400 text-sm">No results found</Text>
                </View>
              ) : (
                results.map((item) => (
                  <Pressable
                    key={item.fdc_id}
                    onPress={() => setSelected(item)}
                    className="px-4 py-3 border-b border-gray-50 active:bg-gray-50"
                  >
                    <Text className="text-sm font-medium text-gray-800 leading-snug" numberOfLines={2}>
                      {item.description}
                    </Text>
                    <View className="flex-row items-center gap-2 mt-0.5">
                      {item.food_category && (
                        <Text className="text-xs text-gray-400">{item.food_category}</Text>
                      )}
                      {item.data_type && (
                        <View className="bg-gray-100 rounded px-1.5 py-0.5">
                          <Text className="text-xs text-gray-500">{item.data_type}</Text>
                        </View>
                      )}
                    </View>
                  </Pressable>
                ))
              )}
            </ScrollView>
          )}
        </View>
      </View>
    </Modal>
  );
}
