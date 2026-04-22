export interface NutrientSummary {
  calories: number;
  protein_g: number;
  fat_g: number;
  carbs_g: number;
  fiber_g: number;
  sugar_g: number;
  saturated_fat_g: number;
  cholesterol_mg: number;
  sodium_mg: number;
  calcium_mg: number;
  iron_mg: number;
  magnesium_mg: number;
  phosphorus_mg: number;
  potassium_mg: number;
  zinc_mg: number;
  vitamin_a_ug: number;
  vitamin_c_mg: number;
  vitamin_d_ug: number;
  vitamin_e_mg: number;
  vitamin_k_ug: number;
  vitamin_b6_mg: number;
  vitamin_b12_ug: number;
  folate_ug: number;
  thiamin_mg: number;
  riboflavin_mg: number;
  niacin_mg: number;
}

export interface MealFood {
  id: string;
  fdc_id: number;
  description: string;
  quantity_g: number;
  serving_description: string | null;
  nutrients: NutrientSummary;
}

export interface Meal {
  id: string;
  name: string;
  meal_type: "breakfast" | "lunch" | "dinner" | "snack";
  scheduled_date: string;
  instructions: string[];
  notes: string | null;
  foods: MealFood[];
  total_nutrition: NutrientSummary;
}

export interface MealPlan {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  meals: Meal[];
  total_nutrition: NutrientSummary;
}

export interface MealPlanSummary {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  meal_count: number;
}

export interface UserProfile {
  user_id: string;
  dietary_restrictions: string[];
  allergies: string[];
  goals: string | null;
  calorie_target: number | null;
  meals_per_day: number;
  preferences: Record<string, unknown>;
  onboarding_complete: boolean;
}

export interface User {
  id: string;
  email: string;
  profile: UserProfile | null;
}

export interface ShoppingItem {
  fdc_id: number;
  description: string;
  total_g: number;
  meals: string[];
}

export interface ChatSession {
  id: string;
  title: string;
  updated_at: string | null;
  created_at: string | null;
  message_count: number;
}

export interface FavoriteMealSummary {
  id: string;
  name: string;
  meal_type: string;
  created_at: string;
  source_meal_id: string | null;
  total_nutrition: NutrientSummary;
}

export interface FavoriteMeal extends FavoriteMealSummary {
  user_id: string;
  instructions: string[];
  notes: string | null;
  foods: MealFood[];
}

export interface FoodSearchResult {
  fdc_id: number;
  description: string;
  food_category: string | null;
  data_type: string | null;
  nutrients_per_100g: NutrientSummary;
}
