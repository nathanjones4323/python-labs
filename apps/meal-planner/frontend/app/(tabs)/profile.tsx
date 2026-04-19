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
import { getUser, upsertProfile } from "../../lib/api";
import { loadUserId } from "../../lib/storage";
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
  const [editPrefs, setEditPrefs] = useState("");

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
    const notesVal = p.preferences?.notes;
    setEditPrefs(typeof notesVal === "string" ? notesVal : "");
  }

  function cancelSection() {
    if (profile) syncBuffers(profile);
    setEditingSection(null);
  }

  async function saveSection(section: string) {
    if (!userId || !profile) return;
    setSaving(true);
    try {
      const updated = await upsertProfile(userId, {
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
        preferences:
          section === "prefs"
            ? (editPrefs ? { notes: editPrefs } : {})
            : profile.preferences,
      });
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
        <Text className="text-gray-500 text-center">Profile not set up yet. Complete onboarding first.</Text>
      </View>
    );
  }

  const isDietEditing = editingSection === "diet";
  const isGoalEditing = editingSection === "goal";
  const isTargetsEditing = editingSection === "targets";
  const isPrefsEditing = editingSection === "prefs";

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

        {/* Other Preferences */}
        <View className="bg-white rounded-2xl p-4">
          <SectionHeader
            title="Other Preferences"
            editing={isPrefsEditing}
            onEdit={() => setEditingSection("prefs")}
            onSave={() => saveSection("prefs")}
            onCancel={cancelSection}
            saving={saving && isPrefsEditing}
          />
          {isPrefsEditing ? (
            <TextInput
              className="border border-gray-200 rounded-xl px-3 py-2 text-sm text-gray-900 bg-gray-50 min-h-[80px]"
              placeholder="e.g. I prefer Mediterranean flavors, dislike cilantro…"
              multiline
              value={editPrefs}
              onChangeText={setEditPrefs}
            />
          ) : (
            <Text className="text-sm text-gray-700">
              {typeof profile.preferences?.notes === "string" && profile.preferences.notes
                ? profile.preferences.notes
                : <Text className="text-gray-400">None specified</Text>}
            </Text>
          )}
        </View>
      </View>
    </ScrollView>
  );
}
