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
import { Ionicons } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { getUser, upsertProfile, patchProfile } from "../../lib/api";
import { loadUserId, clearUserId, clearSessionId } from "../../lib/storage";
import { UserProfile } from "../../lib/types";

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

function SectionHeader({
  title,
  editing,
  onEdit,
  onSave,
  onCancel,
  saving,
}: {
  title: string;
  editing: boolean;
  onEdit: () => void;
  onSave: () => void;
  onCancel: () => void;
  saving?: boolean;
}) {
  return (
    <View className="flex-row items-center justify-between mb-3">
      <Text className="text-xs text-gray-500 font-semibold uppercase tracking-wider">{title}</Text>
      {editing ? (
        <View className="flex-row gap-2">
          <Pressable onPress={onCancel} className="px-3 py-1 rounded-lg bg-gray-100">
            <Text className="text-xs font-semibold text-gray-600">Cancel</Text>
          </Pressable>
          <Pressable
            onPress={onSave}
            disabled={saving}
            className="px-3 py-1 rounded-lg bg-primary-600"
          >
            {saving ? (
              <ActivityIndicator size="small" color="white" />
            ) : (
              <Text className="text-xs font-semibold text-white">Save</Text>
            )}
          </Pressable>
        </View>
      ) : (
        <Pressable onPress={onEdit} className="p-1">
          <Ionicons name="pencil-outline" size={16} color="#9ca3af" />
        </Pressable>
      )}
    </View>
  );
}

export default function ProfileScreen() {
  const router = useRouter();
  const [userId, setUserId] = useState<string | null>(null);
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [email, setEmail] = useState("");

  // Which section is being edited
  const [editingSection, setEditingSection] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Edit buffers (per section)
  const [editRestrictions, setEditRestrictions] = useState<string[]>([]);
  const [editAllergies, setEditAllergies] = useState("");
  const [editGoal, setEditGoal] = useState("");
  const [editCalories, setEditCalories] = useState("");
  const [editMealsPerDay, setEditMealsPerDay] = useState("3");
  // Behavioral prefs
  const [editCookingSkill, setEditCookingSkill] = useState("");
  const [editWeekdayMins, setEditWeekdayMins] = useState("");
  const [editWeekendMins, setEditWeekendMins] = useState("");
  const [editMealPrep, setEditMealPrep] = useState(false);
  const [editLeftovers, setEditLeftovers] = useState(true);
  const [editBudget, setEditBudget] = useState("");

  useEffect(() => {
    async function load() {
      const uid = await loadUserId();
      setUserId(uid);
      if (!uid) { setLoading(false); return; }
      try {
        const user = await getUser(uid);
        setEmail(user.email);
        if (user.profile) {
          setProfile(user.profile);
          syncBuffers(user.profile);
        }
      } catch {
        // silently fail
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  function syncBuffers(p: UserProfile) {
    setEditRestrictions([...p.dietary_restrictions]);
    setEditAllergies(p.allergies.join(", "));
    setEditGoal(p.goals ?? "");
    setEditCalories(p.calorie_target ? String(p.calorie_target) : "");
    setEditMealsPerDay(String(p.meals_per_day ?? 3));
    const b = (p.preferences as Record<string, any>)?.behavioral ?? {};
    setEditCookingSkill(b.cooking_skill ?? "");
    setEditWeekdayMins(b.time_available_weekday_mins != null ? String(b.time_available_weekday_mins) : "");
    setEditWeekendMins(b.time_available_weekend_mins != null ? String(b.time_available_weekend_mins) : "");
    setEditMealPrep(!!b.meal_prep_willing);
    setEditLeftovers(b.leftovers_ok !== false);
    setEditBudget(b.budget ?? "");
  }

  async function signOut() {
    try {
      await clearUserId();
      await clearSessionId();
      router.replace("/auth");
    } catch (e) {
      Alert.alert("Sign out failed", e instanceof Error ? e.message : "Please try again.");
    }
  }

  function cancelSection() {
    if (profile) syncBuffers(profile);
    setEditingSection(null);
  }

  async function saveSection(section: string) {
    if (!userId || !profile) return;
    setSaving(true);
    try {
      let updated: UserProfile;
      if (section === "prefs") {
        updated = await patchProfile(userId, {
          preferences: {
            behavioral: {
              cooking_skill: editCookingSkill || undefined,
              time_available_weekday_mins: editWeekdayMins ? parseInt(editWeekdayMins) : undefined,
              time_available_weekend_mins: editWeekendMins ? parseInt(editWeekendMins) : undefined,
              meal_prep_willing: editMealPrep,
              leftovers_ok: editLeftovers,
              budget: editBudget || undefined,
            },
          },
        });
      } else {
        updated = await upsertProfile(userId, {
          dietary_restrictions:
            section === "diet" ? editRestrictions : profile.dietary_restrictions,
          allergies:
            section === "diet"
              ? editAllergies.split(",").map((s) => s.trim()).filter(Boolean)
              : profile.allergies,
          goals: section === "goal" ? (editGoal || null) : profile.goals,
          calorie_target:
            section === "targets"
              ? (editCalories ? parseInt(editCalories) : null)
              : profile.calorie_target,
          meals_per_day:
            section === "targets"
              ? (parseInt(editMealsPerDay) || 3)
              : profile.meals_per_day,
          preferences: profile.preferences,
        });
      }
      setProfile(updated);
      syncBuffers(updated);
      setEditingSection(null);
    } catch (e) {
      Alert.alert("Save failed", e instanceof Error ? e.message : "Unknown error");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <View className="flex-1 items-center justify-center bg-white">
        <ActivityIndicator color="#16a34a" />
      </View>
    );
  }

  if (!profile) {
    return (
      <View className="flex-1 items-center justify-center bg-white px-8">
        <Text className="text-gray-500 text-center mb-8">Profile not set up yet. Complete onboarding first.</Text>
        <Pressable
          onPress={signOut}
          className="flex-row items-center justify-center gap-2 px-6 py-3 rounded-2xl border border-red-100"
        >
          <Ionicons name="log-out-outline" size={18} color="#ef4444" />
          <Text className="text-red-500 font-semibold text-sm">Sign Out</Text>
        </Pressable>
      </View>
    );
  }

  const isDietEditing = editingSection === "diet";
  const isGoalEditing = editingSection === "goal";
  const isTargetsEditing = editingSection === "targets";
  const isPrefsEditing = editingSection === "prefs";
  const behavioral = (profile?.preferences as Record<string, any>)?.behavioral ?? {};

  return (
    <ScrollView className="flex-1 bg-gray-50">
      {/* Banner */}
      <View className="bg-white px-4 pt-6 pb-5 border-b border-gray-100">
        <View className="flex-row items-center gap-3 mb-3">
          <View className="w-12 h-12 rounded-2xl bg-primary-600 items-center justify-center">
            <Ionicons name="person" size={22} color="white" />
          </View>
          <View>
            <Text className="text-base font-bold text-gray-900">{email}</Text>
            <Text className="text-xs text-gray-400">Your meal planning profile</Text>
          </View>
        </View>
        <View className="bg-primary-50 rounded-xl px-4 py-3">
          <View className="flex-row items-start gap-2">
            <Ionicons name="information-circle-outline" size={16} color="#16a34a" style={{ marginTop: 1 }} />
            <Text className="text-xs text-primary-700 flex-1 leading-relaxed">
              The AI uses these preferences every time it creates a meal plan. Keep them up to date for the best results.
            </Text>
          </View>
        </View>
      </View>

      <View className="px-4 pt-4 pb-8 gap-4">
        {/* Dietary Restrictions & Allergies */}
        <View className="bg-white rounded-2xl p-4">
          <SectionHeader
            title="Diet & Allergies"
            editing={isDietEditing}
            onEdit={() => setEditingSection("diet")}
            onSave={() => saveSection("diet")}
            onCancel={cancelSection}
            saving={saving && isDietEditing}
          />

          {isDietEditing ? (
            <>
              <Text className="text-xs text-gray-500 mb-2">Dietary restrictions</Text>
              <View className="flex-row flex-wrap mb-3">
                {DIETARY_OPTIONS.map((opt) => (
                  <ToggleChip
                    key={opt}
                    label={opt}
                    selected={editRestrictions.includes(opt)}
                    onPress={() =>
                      setEditRestrictions((prev) =>
                        prev.includes(opt) ? prev.filter((r) => r !== opt) : [...prev, opt]
                      )
                    }
                  />
                ))}
              </View>
              <Text className="text-xs text-gray-500 mb-1">Allergies (comma-separated)</Text>
              <TextInput
                className="border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-900 bg-gray-50"
                placeholder="e.g. peanuts, shellfish"
                value={editAllergies}
                onChangeText={setEditAllergies}
              />
            </>
          ) : (
            <>
              {profile.dietary_restrictions.length > 0 ? (
                <>
                  <Text className="text-xs text-gray-400 mb-1.5">Restrictions</Text>
                  <View className="flex-row flex-wrap mb-2">
                    {profile.dietary_restrictions.map((r) => (
                      <View key={r} className="bg-primary-50 border border-primary-200 rounded-full px-3 py-1 mr-2 mb-1">
                        <Text className="text-xs font-medium text-primary-700">{r}</Text>
                      </View>
                    ))}
                  </View>
                </>
              ) : (
                <Text className="text-sm text-gray-400 mb-2">No dietary restrictions</Text>
              )}
              {profile.allergies.length > 0 ? (
                <>
                  <Text className="text-xs text-gray-400 mb-1.5">Allergies</Text>
                  <Text className="text-sm text-gray-700">{profile.allergies.join(", ")}</Text>
                </>
              ) : (
                <Text className="text-sm text-gray-400">No allergies listed</Text>
              )}
            </>
          )}
        </View>

        {/* Health Goal */}
        <View className="bg-white rounded-2xl p-4">
          <SectionHeader
            title="Health Goal"
            editing={isGoalEditing}
            onEdit={() => setEditingSection("goal")}
            onSave={() => saveSection("goal")}
            onCancel={cancelSection}
            saving={saving && isGoalEditing}
          />
          {isGoalEditing ? (
            <View className="flex-row flex-wrap">
              {GOAL_OPTIONS.map((opt) => (
                <ToggleChip
                  key={opt}
                  label={opt}
                  selected={editGoal === opt}
                  onPress={() => setEditGoal(opt)}
                />
              ))}
            </View>
          ) : (
            <Text className="text-sm text-gray-700">
              {profile.goals ?? <Text className="text-gray-400">Not set</Text>}
            </Text>
          )}
        </View>

        {/* Calorie Target & Meals Per Day */}
        <View className="bg-white rounded-2xl p-4">
          <SectionHeader
            title="Daily Targets"
            editing={isTargetsEditing}
            onEdit={() => setEditingSection("targets")}
            onSave={() => saveSection("targets")}
            onCancel={cancelSection}
            saving={saving && isTargetsEditing}
          />
          {isTargetsEditing ? (
            <>
              <Text className="text-xs text-gray-500 mb-1">Daily calorie target (kcal)</Text>
              <TextInput
                className="border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-900 bg-gray-50 mb-3"
                placeholder="e.g. 2000"
                keyboardType="numeric"
                value={editCalories}
                onChangeText={setEditCalories}
              />
              <Text className="text-xs text-gray-500 mb-1">Meals per day</Text>
              <View className="flex-row items-center gap-3">
                <Pressable
                  onPress={() => setEditMealsPerDay((v) => String(Math.max(1, parseInt(v) - 1)))}
                  className="w-9 h-9 rounded-xl bg-gray-100 items-center justify-center"
                >
                  <Ionicons name="remove" size={18} color="#374151" />
                </Pressable>
                <Text className="text-lg font-bold text-gray-900 w-8 text-center">
                  {editMealsPerDay}
                </Text>
                <Pressable
                  onPress={() => setEditMealsPerDay((v) => String(Math.min(8, parseInt(v) + 1)))}
                  className="w-9 h-9 rounded-xl bg-gray-100 items-center justify-center"
                >
                  <Ionicons name="add" size={18} color="#374151" />
                </Pressable>
              </View>
            </>
          ) : (
            <View className="gap-2">
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Daily calories</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {profile.calorie_target ? `${profile.calorie_target.toLocaleString()} kcal` : "Not set"}
                </Text>
              </View>
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Meals per day</Text>
                <Text className="text-sm font-semibold text-gray-800">{profile.meals_per_day}</Text>
              </View>
            </View>
          )}
        </View>

        {/* Cooking Preferences */}
        <View className="bg-white rounded-2xl p-4">
          <SectionHeader
            title="Cooking Preferences"
            editing={isPrefsEditing}
            onEdit={() => setEditingSection("prefs")}
            onSave={() => saveSection("prefs")}
            onCancel={cancelSection}
            saving={saving && isPrefsEditing}
          />
          {isPrefsEditing ? (
            <>
              <Text className="text-xs text-gray-500 mb-2">Cooking skill</Text>
              <View className="flex-row flex-wrap mb-3">
                {["Beginner", "Intermediate", "Advanced"].map((opt) => (
                  <ToggleChip
                    key={opt}
                    label={opt}
                    selected={editCookingSkill === opt}
                    onPress={() => setEditCookingSkill(opt)}
                  />
                ))}
              </View>

              <Text className="text-xs text-gray-500 mb-2">Budget</Text>
              <View className="flex-row flex-wrap mb-3">
                {["Low", "Medium", "High"].map((opt) => (
                  <ToggleChip
                    key={opt}
                    label={opt}
                    selected={editBudget === opt}
                    onPress={() => setEditBudget(opt)}
                  />
                ))}
              </View>

              <View className="flex-row gap-3 mb-3">
                <View className="flex-1">
                  <Text className="text-xs text-gray-500 mb-1">Weekday cook time (min)</Text>
                  <TextInput
                    className="border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-900 bg-gray-50"
                    placeholder="e.g. 30"
                    keyboardType="numeric"
                    value={editWeekdayMins}
                    onChangeText={setEditWeekdayMins}
                  />
                </View>
                <View className="flex-1">
                  <Text className="text-xs text-gray-500 mb-1">Weekend cook time (min)</Text>
                  <TextInput
                    className="border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-900 bg-gray-50"
                    placeholder="e.g. 60"
                    keyboardType="numeric"
                    value={editWeekendMins}
                    onChangeText={setEditWeekendMins}
                  />
                </View>
              </View>

              <View className="flex-row gap-3">
                <Pressable
                  onPress={() => setEditMealPrep((v) => !v)}
                  className={`flex-1 flex-row items-center justify-between px-3 py-2.5 rounded-xl border ${
                    editMealPrep ? "bg-primary-50 border-primary-200" : "bg-gray-50 border-gray-200"
                  }`}
                >
                  <Text className={`text-sm font-medium ${editMealPrep ? "text-primary-700" : "text-gray-500"}`}>
                    Meal prep
                  </Text>
                  <View className={`w-10 h-6 rounded-full ${editMealPrep ? "bg-primary-600" : "bg-gray-300"} items-center justify-center`}>
                    <View className={`w-4 h-4 rounded-full bg-white ${editMealPrep ? "translate-x-2" : "-translate-x-2"}`} />
                  </View>
                </Pressable>
                <Pressable
                  onPress={() => setEditLeftovers((v) => !v)}
                  className={`flex-1 flex-row items-center justify-between px-3 py-2.5 rounded-xl border ${
                    editLeftovers ? "bg-primary-50 border-primary-200" : "bg-gray-50 border-gray-200"
                  }`}
                >
                  <Text className={`text-sm font-medium ${editLeftovers ? "text-primary-700" : "text-gray-500"}`}>
                    Leftovers OK
                  </Text>
                  <View className={`w-10 h-6 rounded-full ${editLeftovers ? "bg-primary-600" : "bg-gray-300"} items-center justify-center`}>
                    <View className={`w-4 h-4 rounded-full bg-white ${editLeftovers ? "translate-x-2" : "-translate-x-2"}`} />
                  </View>
                </Pressable>
              </View>
            </>
          ) : (
            <View className="gap-2">
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Cooking skill</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {behavioral.cooking_skill ?? <Text className="font-normal text-gray-400">Not set</Text>}
                </Text>
              </View>
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Budget</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {behavioral.budget ?? <Text className="font-normal text-gray-400">Not set</Text>}
                </Text>
              </View>
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Weekday cook time</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {behavioral.time_available_weekday_mins != null
                    ? `${behavioral.time_available_weekday_mins} min`
                    : <Text className="font-normal text-gray-400">Not set</Text>}
                </Text>
              </View>
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Weekend cook time</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {behavioral.time_available_weekend_mins != null
                    ? `${behavioral.time_available_weekend_mins} min`
                    : <Text className="font-normal text-gray-400">Not set</Text>}
                </Text>
              </View>
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Meal prep</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {behavioral.meal_prep_willing != null
                    ? (behavioral.meal_prep_willing ? "Yes" : "No")
                    : <Text className="font-normal text-gray-400">Not set</Text>}
                </Text>
              </View>
              <View className="flex-row items-center justify-between">
                <Text className="text-sm text-gray-500">Leftovers OK</Text>
                <Text className="text-sm font-semibold text-gray-800">
                  {behavioral.leftovers_ok !== undefined
                    ? (behavioral.leftovers_ok ? "Yes" : "No")
                    : <Text className="font-normal text-gray-400">Not set</Text>}
                </Text>
              </View>
            </View>
          )}
        </View>

        {/* Sign out */}
        <Pressable
          onPress={signOut}
          className="flex-row items-center justify-center gap-2 bg-white rounded-2xl p-4 border border-red-100"
        >
          <Ionicons name="log-out-outline" size={18} color="#ef4444" />
          <Text className="text-red-500 font-semibold text-sm">Sign Out</Text>
        </Pressable>
      </View>
    </ScrollView>
  );
}
