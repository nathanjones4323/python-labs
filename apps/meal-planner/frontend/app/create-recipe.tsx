import { useState, useEffect } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  Alert,
  Platform,
  Image,
} from "react-native";
import { useRouter, Stack } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { createRecipe, describeRecipe, imageRecipe } from "../lib/api";
import { loadUserId } from "../lib/storage";
import { RecipeAIResult, RecipeAIFood } from "../lib/types";
import FoodSearchModal from "../components/FoodSearchModal";

type Tab = "describe" | "build" | "scan";
type MealType = "breakfast" | "lunch" | "dinner" | "snack";

const MEAL_TYPES: MealType[] = ["breakfast", "lunch", "dinner", "snack"];

// ─── Shared ReviewForm ─────────────────────────────────────────────────────────

interface ReviewFormProps {
  name: string;
  onNameChange: (v: string) => void;
  mealType: MealType;
  onMealTypeChange: (v: MealType) => void;
  instructions: string[];
  onInstructionsChange: (v: string[]) => void;
  notes: string;
  onNotesChange: (v: string) => void;
  foods: RecipeAIFood[];
  onFoodsChange: (v: RecipeAIFood[]) => void;
  onSave: () => void;
  saving: boolean;
}

function ReviewForm({
  name, onNameChange,
  mealType, onMealTypeChange,
  instructions, onInstructionsChange,
  notes, onNotesChange,
  foods, onFoodsChange,
  onSave, saving,
}: ReviewFormProps) {
  const [showFoodSearch, setShowFoodSearch] = useState(false);

  function updateInstruction(index: number, value: string) {
    const next = [...instructions];
    next[index] = value;
    onInstructionsChange(next);
  }

  function addInstruction() {
    onInstructionsChange([...instructions, ""]);
  }

  function removeInstruction(index: number) {
    onInstructionsChange(instructions.filter((_, i) => i !== index));
  }

  function updateFoodQty(index: number, qty: string) {
    const parsed = parseFloat(qty);
    if (isNaN(parsed)) return;
    const next = [...foods];
    next[index] = { ...next[index], quantity_g: parsed };
    onFoodsChange(next);
  }

  function removeFood(index: number) {
    onFoodsChange(foods.filter((_, i) => i !== index));
  }

  return (
    <View className="pb-4">
      {/* Name */}
      <View className="mb-4">
        <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5">
          Recipe Name
        </Text>
        <TextInput
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-sm text-gray-900 bg-white"
          value={name}
          onChangeText={onNameChange}
          placeholder="e.g. Grilled Chicken & Sweet Potato"
          placeholderTextColor="#9ca3af"
        />
      </View>

      {/* Meal type */}
      <View className="mb-4">
        <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5">
          Meal Type
        </Text>
        <View className="flex-row gap-2 flex-wrap">
          {MEAL_TYPES.map((t) => (
            <Pressable
              key={t}
              onPress={() => onMealTypeChange(t)}
              className={`px-3 py-1.5 rounded-full border web:cursor-pointer ${
                mealType === t
                  ? "bg-primary-600 border-primary-600"
                  : "bg-white border-gray-200 web:hover:border-primary-300"
              }`}
            >
              <Text
                className={`text-xs font-semibold capitalize ${
                  mealType === t ? "text-white" : "text-gray-600"
                }`}
              >
                {t}
              </Text>
            </Pressable>
          ))}
        </View>
      </View>

      {/* Ingredients */}
      <View className="mb-4">
        <View className="flex-row items-center justify-between mb-1.5">
          <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
            Ingredients
          </Text>
          <Pressable
            onPress={() => setShowFoodSearch(true)}
            className="flex-row items-center gap-1 web:cursor-pointer"
          >
            <Ionicons name="add-circle-outline" size={16} color="#16a34a" />
            <Text className="text-xs font-semibold text-primary-600">Add</Text>
          </Pressable>
        </View>

        {foods.length === 0 ? (
          <Pressable
            onPress={() => setShowFoodSearch(true)}
            className="border border-dashed border-gray-300 rounded-xl py-4 items-center web:cursor-pointer"
          >
            <Text className="text-sm text-gray-400">Tap to add ingredients</Text>
          </Pressable>
        ) : (
          <View className="border border-gray-100 rounded-xl overflow-hidden">
            {foods.map((food, i) => (
              <View
                key={`${food.fdc_id}-${i}`}
                className={`flex-row items-center px-3 py-2.5 ${
                  i < foods.length - 1 ? "border-b border-gray-50" : ""
                }`}
              >
                <Text
                  className="flex-1 text-sm text-gray-800 mr-2"
                  numberOfLines={1}
                >
                  {food.description}
                </Text>
                <TextInput
                  className="border border-gray-200 rounded-lg px-2 py-1 text-xs text-gray-700 w-16 text-center"
                  keyboardType="numeric"
                  value={String(Math.round(food.quantity_g))}
                  onChangeText={(v) => updateFoodQty(i, v)}
                />
                <Text className="text-xs text-gray-400 ml-1 mr-2">g</Text>
                <Pressable onPress={() => removeFood(i)} className="p-1 web:cursor-pointer">
                  <Ionicons name="close-circle-outline" size={16} color="#9ca3af" />
                </Pressable>
              </View>
            ))}
          </View>
        )}
      </View>

      {/* Instructions */}
      <View className="mb-4">
        <View className="flex-row items-center justify-between mb-1.5">
          <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
            Instructions
          </Text>
          <Pressable
            onPress={addInstruction}
            className="flex-row items-center gap-1 web:cursor-pointer"
          >
            <Ionicons name="add-circle-outline" size={16} color="#16a34a" />
            <Text className="text-xs font-semibold text-primary-600">Add Step</Text>
          </Pressable>
        </View>

        {instructions.map((step, i) => (
          <View key={i} className="flex-row items-start mb-2">
            <View className="w-6 h-6 rounded-full bg-primary-100 items-center justify-center mr-2 mt-1 flex-shrink-0">
              <Text className="text-primary-700 text-xs font-bold">{i + 1}</Text>
            </View>
            <TextInput
              className="flex-1 border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-900 bg-white"
              value={step}
              onChangeText={(v) => updateInstruction(i, v)}
              placeholder={`Step ${i + 1}`}
              placeholderTextColor="#9ca3af"
              multiline
            />
            <Pressable
              onPress={() => removeInstruction(i)}
              className="ml-2 mt-1.5 p-1 web:cursor-pointer"
            >
              <Ionicons name="close-circle-outline" size={16} color="#9ca3af" />
            </Pressable>
          </View>
        ))}
        {instructions.length === 0 && (
          <Pressable
            onPress={addInstruction}
            className="border border-dashed border-gray-300 rounded-xl py-3 items-center web:cursor-pointer"
          >
            <Text className="text-sm text-gray-400">Add cooking steps</Text>
          </Pressable>
        )}
      </View>

      {/* Notes */}
      <View className="mb-6">
        <Text className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1.5">
          Notes (optional)
        </Text>
        <TextInput
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-sm text-gray-900 bg-white"
          value={notes}
          onChangeText={onNotesChange}
          placeholder="Meal prep tips, substitutions, etc."
          placeholderTextColor="#9ca3af"
          multiline
          numberOfLines={3}
          style={{ minHeight: 72, textAlignVertical: "top" }}
        />
      </View>

      {/* Save button */}
      <Pressable
        onPress={onSave}
        disabled={saving || !name.trim() || foods.length === 0}
        className={`rounded-xl py-3.5 items-center web:cursor-pointer ${
          saving || !name.trim() || foods.length === 0
            ? "bg-gray-200"
            : "bg-primary-600 active:opacity-80"
        }`}
      >
        {saving ? (
          <ActivityIndicator color="#fff" />
        ) : (
          <Text
            className={`font-semibold text-sm ${
              !name.trim() || foods.length === 0 ? "text-gray-400" : "text-white"
            }`}
          >
            Save Recipe
          </Text>
        )}
      </Pressable>

      <FoodSearchModal
        visible={showFoodSearch}
        onClose={() => setShowFoodSearch(false)}
        onSelect={(food) => {
          onFoodsChange([
            ...foods,
            {
              fdc_id: food.fdc_id,
              description: food.description,
              quantity_g: food.quantity_g,
              serving_description: undefined,
            },
          ]);
        }}
      />
    </View>
  );
}

// ─── Describe tab ──────────────────────────────────────────────────────────────

function DescribeTab({ onResult }: { onResult: (r: RecipeAIResult) => void }) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleAnalyze() {
    if (!text.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const uid = await loadUserId();
      if (!uid) throw new Error("Not logged in");
      const result = await describeRecipe(uid, text.trim());
      onResult(result);
    } catch (e: any) {
      setError(e.message ?? "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <View>
      <Text className="text-sm text-gray-500 mb-3 leading-5">
        Describe your meal in natural language and let AI identify the ingredients
        and build the recipe for you.
      </Text>
      <TextInput
        className="border border-gray-200 rounded-xl px-3 py-3 text-sm text-gray-900 bg-white mb-3"
        value={text}
        onChangeText={setText}
        placeholder="e.g. Grilled chicken breast with roasted sweet potato and steamed broccoli"
        placeholderTextColor="#9ca3af"
        multiline
        numberOfLines={4}
        style={{ minHeight: 96, textAlignVertical: "top" }}
      />
      {error && (
        <Text className="text-sm text-red-500 mb-3">{error}</Text>
      )}
      <Pressable
        onPress={handleAnalyze}
        disabled={loading || !text.trim()}
        className={`rounded-xl py-3.5 items-center web:cursor-pointer ${
          loading || !text.trim() ? "bg-gray-200" : "bg-primary-600 active:opacity-80"
        }`}
      >
        {loading ? (
          <View className="flex-row items-center gap-2">
            <ActivityIndicator color="#fff" size="small" />
            <Text className="text-white text-sm font-semibold">Analyzing…</Text>
          </View>
        ) : (
          <Text
            className={`font-semibold text-sm ${
              !text.trim() ? "text-gray-400" : "text-white"
            }`}
          >
            Analyze with AI
          </Text>
        )}
      </Pressable>
    </View>
  );
}

// ─── Scan tab ──────────────────────────────────────────────────────────────────

function ScanTab({ onResult }: { onResult: (r: RecipeAIResult) => void }) {
  const [imageUri, setImageUri] = useState<string | null>(null);
  const [imageBase64, setImageBase64] = useState<string | null>(null);
  const [mediaType, setMediaType] = useState("image/jpeg");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function readFile(file: File) {
    setMediaType(file.type || "image/jpeg");
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result as string;
      setImageBase64(dataUrl.split(",")[1]);
      setImageUri(dataUrl);
    };
    reader.readAsDataURL(file);
  }

  function pickImageWeb() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.onchange = () => {
      const file = input.files?.[0];
      if (file) readFile(file);
    };
    input.click();
  }

  async function pickImageNative() {
    try {
      const ImagePicker = await import("expo-image-picker").then((m) => m);
      const result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ["images"],
        base64: true,
        quality: 0.8,
      });
      if (!result.canceled && result.assets[0]) {
        const asset = result.assets[0];
        setImageUri(asset.uri);
        setImageBase64(asset.base64 ?? null);
        const ext = asset.uri.split(".").pop()?.toLowerCase();
        setMediaType(ext === "png" ? "image/png" : "image/jpeg");
      }
    } catch {
      setError("Image picker not available. Please use the web version.");
    }
  }

  async function handleExtract() {
    if (!imageBase64) return;
    setLoading(true);
    setError(null);
    try {
      const uid = await loadUserId();
      if (!uid) throw new Error("Not logged in");
      const result = await imageRecipe(uid, imageBase64, mediaType);
      onResult(result);
    } catch (e: any) {
      setError(e.message ?? "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <View>
      <Text className="text-sm text-gray-500 mb-3 leading-5">
        Upload a recipe image or photo of your meal and AI will extract the
        ingredients and instructions for you.
      </Text>

      {/* Image picker */}
      {imageUri ? (
        <View className="mb-3">
          <Image
            source={{ uri: imageUri }}
            className="w-full rounded-xl"
            style={{ height: 200 }}
            resizeMode="cover"
          />
          <Pressable
            onPress={() => {
              setImageUri(null);
              setImageBase64(null);
            }}
            className="mt-2 flex-row items-center gap-1 web:cursor-pointer"
          >
            <Ionicons name="close-circle-outline" size={16} color="#6b7280" />
            <Text className="text-xs text-gray-500">Remove image</Text>
          </Pressable>
        </View>
      ) : (
        <Pressable
          onPress={Platform.OS === "web" ? pickImageWeb : pickImageNative}
          className="border-2 border-dashed border-gray-300 rounded-xl py-8 items-center mb-3 web:cursor-pointer web:hover:border-primary-400"
        >
          <Ionicons name="image-outline" size={28} color="#9ca3af" />
          <Text className="text-sm text-gray-400 mt-2">
            {Platform.OS === "web" ? "Click to choose an image" : "Tap to choose an image"}
          </Text>
        </Pressable>
      )}

      {error && (
        <Text className="text-sm text-red-500 mb-3">{error}</Text>
      )}

      <Pressable
        onPress={handleExtract}
        disabled={loading || !imageBase64}
        className={`rounded-xl py-3.5 items-center web:cursor-pointer ${
          loading || !imageBase64 ? "bg-gray-200" : "bg-primary-600 active:opacity-80"
        }`}
      >
        {loading ? (
          <View className="flex-row items-center gap-2">
            <ActivityIndicator color="#fff" size="small" />
            <Text className="text-white text-sm font-semibold">Extracting…</Text>
          </View>
        ) : (
          <Text
            className={`font-semibold text-sm ${
              !imageBase64 ? "text-gray-400" : "text-white"
            }`}
          >
            Extract Recipe
          </Text>
        )}
      </Pressable>
    </View>
  );
}

// ─── Main screen ───────────────────────────────────────────────────────────────

function emptyForm() {
  return {
    name: "",
    mealType: "dinner" as MealType,
    instructions: [""],
    notes: "",
    foods: [] as RecipeAIFood[],
  };
}

function resultToForm(r: RecipeAIResult) {
  return {
    name: r.name,
    mealType: r.meal_type,
    instructions: r.instructions.length > 0 ? r.instructions : [""],
    notes: r.notes ?? "",
    foods: r.foods,
  };
}

export default function CreateRecipeScreen() {
  const router = useRouter();
  const [activeTab, setActiveTab] = useState<Tab>("describe");
  const [reviewMode, setReviewMode] = useState(false);
  const [saving, setSaving] = useState(false);

  const [form, setForm] = useState(emptyForm());

  function patch(updates: Partial<ReturnType<typeof emptyForm>>) {
    setForm((prev) => ({ ...prev, ...updates }));
  }

  function handleAIResult(result: RecipeAIResult) {
    setForm(resultToForm(result));
    setReviewMode(true);
  }

  async function handleSave() {
    const uid = await loadUserId();
    if (!uid) return;
    if (!form.name.trim() || form.foods.length === 0) return;

    setSaving(true);
    try {
      await createRecipe(uid, {
        name: form.name.trim(),
        meal_type: form.mealType,
        instructions: form.instructions.filter((s) => s.trim()),
        notes: form.notes.trim() || undefined,
        foods: form.foods.map((f) => ({
          fdc_id: f.fdc_id,
          quantity_g: f.quantity_g,
          serving_description: f.serving_description,
        })),
      });
      router.replace("/(tabs)/recipes");
    } catch (e: any) {
      Alert.alert("Error", e.message ?? "Failed to save recipe");
    } finally {
      setSaving(false);
    }
  }

  const TABS: { key: Tab; label: string; icon: string }[] = [
    { key: "describe", label: "Describe", icon: "chatbubble-outline" },
    { key: "build", label: "Build", icon: "construct-outline" },
    { key: "scan", label: "Scan", icon: "camera-outline" },
  ];

  return (
    <ScrollView
      className="flex-1 bg-gray-50"
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={{ padding: 16 }}
    >
      <Stack.Screen
        options={{
          headerLeft: () => (
            <Pressable
              onPress={() => router.replace("/(tabs)/recipes")}
              className="pl-1 pr-3 py-1 web:cursor-pointer"
            >
              <Ionicons name="arrow-back" size={22} color="#374151" />
            </Pressable>
          ),
        }}
      />
      {/* Tab switcher — hidden when in review mode */}
      {!reviewMode && (
        <View className="flex-row bg-white rounded-2xl p-1 mb-5 shadow-sm"
          style={{ shadowColor: "#000", shadowOpacity: 0.04, shadowRadius: 4, shadowOffset: { width: 0, height: 1 } }}
        >
          {TABS.map(({ key, label, icon }) => (
            <Pressable
              key={key}
              onPress={() => setActiveTab(key)}
              className={`flex-1 flex-row items-center justify-center gap-1.5 py-2.5 rounded-xl web:cursor-pointer ${
                activeTab === key ? "bg-primary-600" : ""
              }`}
            >
              <Ionicons
                name={icon as any}
                size={15}
                color={activeTab === key ? "#fff" : "#6b7280"}
              />
              <Text
                className={`text-sm font-semibold ${
                  activeTab === key ? "text-white" : "text-gray-500"
                }`}
              >
                {label}
              </Text>
            </Pressable>
          ))}
        </View>
      )}

      {/* Review mode header */}
      {reviewMode && (
        <View className="flex-row items-center mb-4">
          <Pressable
            onPress={() => setReviewMode(false)}
            className="mr-3 p-1 web:cursor-pointer"
          >
            <Ionicons name="arrow-back" size={20} color="#374151" />
          </Pressable>
          <Text className="text-base font-semibold text-gray-900">Review & Edit</Text>
        </View>
      )}

      {/* Content */}
      <View className="bg-white rounded-2xl p-4 shadow-sm"
        style={{ shadowColor: "#000", shadowOpacity: 0.04, shadowRadius: 4, shadowOffset: { width: 0, height: 1 } }}
      >
        {reviewMode || activeTab === "build" ? (
          <ReviewForm
            name={form.name}
            onNameChange={(v) => patch({ name: v })}
            mealType={form.mealType}
            onMealTypeChange={(v) => patch({ mealType: v })}
            instructions={form.instructions}
            onInstructionsChange={(v) => patch({ instructions: v })}
            notes={form.notes}
            onNotesChange={(v) => patch({ notes: v })}
            foods={form.foods}
            onFoodsChange={(v) => patch({ foods: v })}
            onSave={handleSave}
            saving={saving}
          />
        ) : activeTab === "describe" ? (
          <DescribeTab onResult={handleAIResult} />
        ) : (
          <ScanTab onResult={handleAIResult} />
        )}
      </View>
    </ScrollView>
  );
}
