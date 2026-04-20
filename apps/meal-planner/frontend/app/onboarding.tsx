import { useEffect, useState } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  Alert,
} from "react-native";
import { useRouter } from "expo-router";
import { upsertProfile } from "../lib/api";
import { loadUserId } from "../lib/storage";

const DIETARY_OPTIONS = [
  "Vegetarian",
  "Vegan",
  "Gluten-Free",
  "Dairy-Free",
  "Nut-Free",
  "Halal",
  "Kosher",
  "Paleo",
  "Keto",
  "Low-FODMAP",
];

const GOAL_OPTIONS = [
  "Weight loss",
  "Muscle gain",
  "Maintenance",
  "Heart health",
  "Better energy",
  "Reduce inflammation",
  "Athletic performance",
];

function ToggleChip({
  label,
  selected,
  onPress,
}: {
  label: string;
  selected: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      className={`px-3 py-1.5 rounded-full border mr-2 mb-2 ${
        selected ? "bg-primary-600 border-primary-600" : "bg-white border-gray-300"
      }`}
    >
      <Text className={`text-sm font-medium ${selected ? "text-white" : "text-gray-700"}`}>
        {label}
      </Text>
    </Pressable>
  );
}

type Step = "restrictions" | "goals" | "details";

export default function OnboardingScreen() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("restrictions");
  const [loading, setLoading] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);

  const [restrictions, setRestrictions] = useState<string[]>([]);
  const [allergies, setAllergies] = useState("");
  const [goal, setGoal] = useState("");
  const [calories, setCalories] = useState("");
  const [mealsPerDay, setMealsPerDay] = useState("3");
  const [otherPrefs, setOtherPrefs] = useState("");

  useEffect(() => {
    loadUserId().then(setUserId);
  }, []);

  function toggleRestriction(opt: string) {
    setRestrictions((prev) =>
      prev.includes(opt) ? prev.filter((r) => r !== opt) : [...prev, opt]
    );
  }

  async function finish() {
    if (!userId) return;
    setLoading(true);
    try {
      await upsertProfile(userId, {
        dietary_restrictions: restrictions,
        allergies: allergies
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        goals: goal || null,
        calorie_target: calories ? parseInt(calories) : null,
        meals_per_day: parseInt(mealsPerDay) || 3,
        preferences: otherPrefs ? { notes: otherPrefs } : {},
      });
      router.replace("/(tabs)/chat");
    } catch (e: unknown) {
      Alert.alert("Error", e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <ScrollView className="flex-1 bg-white">
      <View className="flex-1 px-6 py-12 max-w-lg mx-auto w-full">
        <Text className="text-3xl font-bold text-gray-900 mb-2">One last step</Text>
        <Text className="text-gray-500 mb-8">
          Tell us about your preferences so we can personalize your meal plans.
        </Text>

        {/* Step: Dietary Restrictions */}
        {step === "restrictions" && (
          <View>
            <Text className="text-xl font-semibold text-gray-800 mb-1">
              Any dietary restrictions?
            </Text>
            <Text className="text-gray-400 text-sm mb-4">Select all that apply.</Text>
            <View className="flex-row flex-wrap mb-4">
              {DIETARY_OPTIONS.map((opt) => (
                <ToggleChip
                  key={opt}
                  label={opt}
                  selected={restrictions.includes(opt)}
                  onPress={() => toggleRestriction(opt)}
                />
              ))}
            </View>
            <Text className="text-sm font-medium text-gray-700 mb-1">
              Allergies (comma-separated)
            </Text>
            <TextInput
              className="border border-gray-300 rounded-xl px-4 py-3 text-base text-gray-900 mb-4"
              placeholder="e.g. peanuts, shellfish"
              value={allergies}
              onChangeText={setAllergies}
            />
            <Pressable
              className="bg-primary-600 rounded-xl py-3 items-center"
              onPress={() => setStep("goals")}
            >
              <Text className="text-white font-semibold text-base">Continue</Text>
            </Pressable>
          </View>
        )}

        {/* Step: Goals */}
        {step === "goals" && (
          <View>
            <Text className="text-xl font-semibold text-gray-800 mb-1">
              What's your health goal?
            </Text>
            <Text className="text-gray-400 text-sm mb-4">Choose the most important one.</Text>
            <View className="flex-row flex-wrap mb-4">
              {GOAL_OPTIONS.map((opt) => (
                <ToggleChip
                  key={opt}
                  label={opt}
                  selected={goal === opt}
                  onPress={() => setGoal(opt)}
                />
              ))}
            </View>
            <Pressable
              className="bg-primary-600 rounded-xl py-3 items-center"
              onPress={() => setStep("details")}
            >
              <Text className="text-white font-semibold text-base">Continue</Text>
            </Pressable>
          </View>
        )}

        {/* Step: Details */}
        {step === "details" && (
          <View>
            <Text className="text-xl font-semibold text-gray-800 mb-4">
              A few more details
            </Text>

            <Text className="text-sm font-medium text-gray-700 mb-1">
              Daily calorie target (optional)
            </Text>
            <TextInput
              className="border border-gray-300 rounded-xl px-4 py-3 text-base text-gray-900 mb-3"
              placeholder="e.g. 2000"
              keyboardType="numeric"
              value={calories}
              onChangeText={setCalories}
            />

            <Text className="text-sm font-medium text-gray-700 mb-1">Meals per day</Text>
            <TextInput
              className="border border-gray-300 rounded-xl px-4 py-3 text-base text-gray-900 mb-3"
              keyboardType="numeric"
              value={mealsPerDay}
              onChangeText={setMealsPerDay}
            />

            <Text className="text-sm font-medium text-gray-700 mb-1">
              Other preferences (optional)
            </Text>
            <TextInput
              className="border border-gray-300 rounded-xl px-4 py-3 text-base text-gray-900 mb-6"
              placeholder="e.g. I dislike cilantro, prefer Mediterranean flavors"
              multiline
              numberOfLines={3}
              value={otherPrefs}
              onChangeText={setOtherPrefs}
            />

            <Pressable
              className="bg-primary-600 rounded-xl py-3 items-center"
              onPress={finish}
              disabled={loading}
            >
              {loading ? (
                <ActivityIndicator color="white" />
              ) : (
                <Text className="text-white font-semibold text-base">Get Started</Text>
              )}
            </Pressable>
          </View>
        )}

        {/* Progress dots — 3 steps */}
        <View className="flex-row justify-center gap-2 mt-8">
          {(["restrictions", "goals", "details"] as Step[]).map((s) => (
            <View
              key={s}
              className={`w-2 h-2 rounded-full ${step === s ? "bg-primary-600" : "bg-gray-200"}`}
            />
          ))}
        </View>
      </View>
    </ScrollView>
  );
}
