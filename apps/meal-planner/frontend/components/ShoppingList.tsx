import { View, Text } from "react-native";
import { MealPlan, ShoppingItem } from "../lib/types";
import { gramsToConsumer, friendlyName } from "../lib/formatIngredient";

// ── Store section definitions in aisle order ────────────────────────────────

const SECTIONS: { id: string; emoji: string; label: string }[] = [
  { id: "produce",   emoji: "🥬", label: "Produce" },
  { id: "meat",      emoji: "🥩", label: "Meat & Seafood" },
  { id: "dairy",     emoji: "🥛", label: "Dairy & Eggs" },
  { id: "grains",    emoji: "🌾", label: "Grains & Bread" },
  { id: "pantry",    emoji: "🫙", label: "Pantry & Canned" },
  { id: "nuts",      emoji: "🥜", label: "Nuts & Seeds" },
  { id: "spices",    emoji: "🧂", label: "Spices & Herbs" },
  { id: "other",     emoji: "📦", label: "Other" },
];

// Map USDA food_category values to section IDs
const USDA_CATEGORY_MAP: Record<string, string> = {
  "Vegetables and Vegetable Products": "produce",
  "Fruits and Fruit Juices": "produce",
  "Beef Products": "meat",
  "Poultry Products": "meat",
  "Pork Products": "meat",
  "Lamb, Veal, and Game Products": "meat",
  "Finfish and Shellfish Products": "meat",
  "Game Meats": "meat",
  "Sausages and Luncheon Meats": "meat",
  "Dairy and Egg Products": "dairy",
  "Cereal Grains and Pasta": "grains",
  "Baked Products": "grains",
  "Legumes and Legume Products": "pantry",
  "Nut and Seed Products": "nuts",
  "Spices and Herbs": "spices",
  "Fats and Oils": "pantry",
  "Sweets": "pantry",
  "Soups, Sauces, and Gravies": "pantry",
  "Beverages": "other",
  "Snacks": "other",
  "Restaurant Foods": "other",
  "Fast Foods": "other",
  "Meals, Entrees, and Side Dishes": "other",
  "American Indian/Alaska Native Foods": "other",
  "Baby Foods": "other",
};

// Keyword fallback when food_category is absent
function sectionFromDescription(d: string): string {
  // Spices — check early to avoid matching "salt" as produce etc.
  if (d.includes("spice") || d.includes("cumin") || d.includes("paprika") ||
      d.includes("cinnamon") || d.includes("turmeric") || d.includes("oregano") ||
      d.includes("thyme") || d.includes("rosemary") || d.includes("cayenne") ||
      d.includes("chili powder") || d.includes("garlic powder") || d.includes("onion powder") ||
      d.includes("bay leaf") || d.includes("black pepper") || d.includes("white pepper") ||
      d.includes("dried herb") || d.includes("ground clove") || d.includes("nutmeg") ||
      d.includes("cardamom") || d.includes("coriander") || d.includes("fennel seed") ||
      d.includes("anise") || d.includes("allspice")) {
    return "spices";
  }
  // Meat & Seafood
  if (d.includes("chicken") || d.includes("beef") || d.includes("pork") ||
      d.includes("turkey") || d.includes("lamb") || d.includes("bison") ||
      d.includes("salmon") || d.includes("tuna") || d.includes("shrimp") ||
      d.includes("tilapia") || d.includes("cod") || d.includes("halibut") ||
      d.includes("sardine") || d.includes("scallop") || d.includes("crab") ||
      d.includes("lobster") || d.includes("veal") || d.includes("venison") ||
      d.includes("duck") || d.includes("anchov") || d.includes("mackerel") ||
      d.includes("trout") || d.includes("clam") || d.includes("oyster") ||
      d.includes("mussel") || (d.includes("fish") && !d.includes("starfish"))) {
    return "meat";
  }
  // Dairy & Eggs
  if ((d.includes("milk") && !d.includes("coconut milk")) ||
      d.includes("cheese") || d.includes("yogurt") || d.includes("kefir") ||
      (d.includes("cream") && !d.includes("ice cream") && !d.includes("coconut cream")) ||
      d.includes("ricotta") || d.includes("parmesan") || d.includes("mozzarella") ||
      d.includes("cheddar") || d.includes("feta") || d.includes("brie") ||
      d.includes("cottage cheese") || d.includes("sour cream") ||
      (d.includes("butter") && !d.includes("peanut butter") && !d.includes("almond butter") &&
       !d.includes("cashew butter") && !d.includes("sunflower butter")) ||
      (d.includes("egg") && !d.includes("eggplant"))) {
    return "dairy";
  }
  // Grains & Bread
  if (d.includes("bread") || d.includes("tortilla") || d.includes("pita") ||
      d.includes("naan") || d.includes("wrap") || d.includes("roll,") ||
      d.includes("rice") || d.includes("oat") || d.includes("quinoa") ||
      d.includes("pasta") || d.includes("noodle") || d.includes("flour") ||
      d.includes("barley") || d.includes("farro") || d.includes("millet") ||
      d.includes("cornmeal") || d.includes("polenta") || d.includes("couscous") ||
      d.includes("breadcrumb") || d.includes("cracker") || d.includes("cereal") ||
      d.includes("wheat germ") || d.includes("bran")) {
    return "grains";
  }
  // Nuts & Seeds (before pantry to catch nut butters)
  if (d.includes("almond") || d.includes("cashew") || d.includes("walnut") ||
      d.includes("pecan") || d.includes("pistachio") || d.includes("macadamia") ||
      d.includes("pine nut") || d.includes("hemp seed") || d.includes("chia") ||
      d.includes("flax") || d.includes("sunflower seed") || d.includes("pumpkin seed") ||
      d.includes("sesame") || d.includes("tahini") || d.includes("peanut") ||
      d.includes("nut butter") || d.includes("seed butter") ||
      (d.includes("nut") && !d.includes("coconut") && !d.includes("chestnut"))) {
    return "nuts";
  }
  // Pantry
  if (d.includes("oil") || d.includes("vinegar") || d.includes("sauce") ||
      d.includes("broth") || d.includes("stock") || d.includes("honey") ||
      d.includes("maple") || d.includes("syrup") || d.includes("sugar") ||
      d.includes("molasses") || d.includes("lentil") || d.includes("chickpea") ||
      d.includes("canned") || d.includes("coconut milk") || d.includes("coconut cream") ||
      d.includes("coconut flake") || d.includes("raisin") || d.includes("dried cranberr") ||
      d.includes("dried fruit") || d.includes("soy sauce") || d.includes("tamari") ||
      d.includes("miso") || d.includes("worcestershire") || d.includes("hot sauce") ||
      d.includes("mustard") || d.includes("ketchup") || d.includes("mayonnaise") ||
      (d.includes("bean") && !d.includes("green bean") && !d.includes("string bean"))) {
    return "pantry";
  }
  // Everything else → produce (fruits, vegetables, fresh herbs)
  return "produce";
}

function getSection(foodCategory: string | null, description: string): string {
  if (foodCategory) {
    const mapped = USDA_CATEGORY_MAP[foodCategory];
    if (mapped) return mapped;
  }
  return sectionFromDescription(description.toLowerCase());
}

// ── Shopping list builder ────────────────────────────────────────────────────

interface GroupedItem extends ShoppingItem {
  section: string;
}

function buildShoppingList(plan: MealPlan): GroupedItem[] {
  const map = new Map<number, GroupedItem>();
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
          section: getSection(food.food_category, food.description),
        });
      }
    }
  }
  const sectionOrder = Object.fromEntries(SECTIONS.map((s, i) => [s.id, i]));
  return Array.from(map.values()).sort((a, b) => {
    const sectionDiff = (sectionOrder[a.section] ?? 99) - (sectionOrder[b.section] ?? 99);
    if (sectionDiff !== 0) return sectionDiff;
    return a.description.localeCompare(b.description);
  });
}

// ── Component ────────────────────────────────────────────────────────────────

interface Props {
  plan: MealPlan;
}

export default function ShoppingList({ plan }: Props) {
  const items = buildShoppingList(plan);

  // Group into sections, preserving store order
  const sectionMap = new Map<string, GroupedItem[]>();
  for (const item of items) {
    const arr = sectionMap.get(item.section) ?? [];
    arr.push(item);
    sectionMap.set(item.section, arr);
  }

  const activeSections = SECTIONS.filter((s) => sectionMap.has(s.id));

  return (
    <View>
      <Text className="text-xl font-bold text-gray-900 mb-1">{plan.name}</Text>
      <Text className="text-sm text-gray-500 mb-4">{items.length} ingredients</Text>

      {activeSections.map((section) => (
        <View key={section.id} className="mb-4">
          {/* Section header */}
          <View className="flex-row items-center gap-2 mb-1 pb-1 border-b border-gray-200">
            <Text style={{ fontSize: 14 }}>{section.emoji}</Text>
            <Text className="text-xs font-semibold text-gray-600 uppercase tracking-wider">
              {section.label}
            </Text>
          </View>

          {sectionMap.get(section.id)!.map((item) => {
            const { label, hint } = gramsToConsumer(item.total_g, item.description);
            return (
              <View
                key={item.fdc_id}
                className="flex-row items-start justify-between py-2.5 border-b border-gray-100"
              >
                <View className="flex-1 mr-4">
                  <Text className="text-sm font-medium text-gray-900">
                    {friendlyName(item.description)}
                  </Text>
                  <Text className="text-xs text-gray-400 mt-0.5">
                    {item.meals.join(", ")}
                  </Text>
                </View>
                <View className="items-end">
                  <Text className="text-sm font-semibold text-gray-800">{label}</Text>
                  {!!hint && (
                    <Text className="text-xs text-gray-400">{hint}</Text>
                  )}
                </View>
              </View>
            );
          })}
        </View>
      ))}
    </View>
  );
}
