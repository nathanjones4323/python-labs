import { View, Text } from "react-native";
import { MealPlan, ShoppingItem } from "../lib/types";

function buildShoppingList(plan: MealPlan): ShoppingItem[] {
  const map = new Map<number, ShoppingItem>();
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
        });
      }
    }
  }
  return Array.from(map.values()).sort((a, b) =>
    a.description.localeCompare(b.description)
  );
}

/** Snap a decimal cups value to a fraction string, e.g. 0.48 → "½ cup" */
function formatCups(cups: number): string {
  if (cups <= 0.15) return "1 tbsp";
  if (cups <= 0.3)  return "¼ cup";
  if (cups <= 0.6)  return "½ cup";
  if (cups <= 0.9)  return "¾ cup";
  const whole = Math.round(cups);
  return `${whole} cup${whole !== 1 ? "s" : ""}`;
}

/** Round tbsp to nearest whole, with ½ snap */
function formatTbsp(tbsp: number): string {
  const rounded = Math.round(tbsp * 2) / 2; // snap to 0.5
  if (rounded < 1) return "1 tbsp";
  if (rounded === 0.5) return "½ tbsp";
  const whole = Math.floor(rounded);
  const half = rounded % 1 > 0;
  return half ? `${whole}½ tbsp` : `${whole} tbsp`;
}

/**
 * Convert a gram amount to a consumer-friendly quantity string.
 * Returns a primary label (e.g. "2 tbsp") and a secondary hint (e.g. "28g").
 */
function gramsToConsumer(g: number, description: string): { label: string; hint: string } {
  const d = description.toLowerCase();
  const gStr = `${Math.round(g)}g`;

  // ── Oils & fats ──────────────────────────────────────────────
  if (d.includes("oil") || d.includes("ghee") ||
      (d.includes("butter") && !d.includes("peanut") && !d.includes("nut"))) {
    if (g <= 5) return { label: "1 tsp", hint: gStr };
    if (g <= 60) return { label: formatTbsp(g / 14), hint: gStr };
    return { label: formatCups(g / 225), hint: gStr };
  }

  // ── Honey, maple syrup, agave ────────────────────────────────
  if (d.includes("honey") || d.includes("maple") || d.includes("syrup") || d.includes("agave")) {
    if (g <= 5) return { label: "1 tsp", hint: gStr };
    return { label: formatTbsp(g / 21), hint: gStr };
  }

  // ── Liquid dairy (milk, cream, kefir) ────────────────────────
  if ((d.includes("milk") || d.includes("cream") || d.includes("kefir")) && !d.includes("powder")) {
    return { label: formatCups(g / 240), hint: gStr };
  }

  // ── Yogurt, sour cream, cottage cheese ───────────────────────
  if (d.includes("yogurt") || d.includes("sour cream") || d.includes("cottage")) {
    return { label: formatCups(g / 245), hint: gStr };
  }

  // ── Eggs ─────────────────────────────────────────────────────
  if (d.includes("egg") && !d.includes("eggplant")) {
    const eggs = Math.max(1, Math.round(g / 50));
    return { label: `${eggs} egg${eggs !== 1 ? "s" : ""}`, hint: gStr };
  }

  // ── Meat, poultry, seafood ───────────────────────────────────
  if (d.includes("chicken") || d.includes("beef") || d.includes("pork") ||
      d.includes("turkey") || d.includes("lamb") || d.includes("bison") ||
      d.includes("fish") || d.includes("salmon") || d.includes("tuna") ||
      d.includes("shrimp") || d.includes("tilapia") || d.includes("cod") ||
      d.includes("halibut") || d.includes("sardine") || d.includes("scallop") ||
      d.includes("crab") || d.includes("lobster")) {
    const oz = g / 28.35;
    if (oz >= 32) return { label: `${(oz / 16).toFixed(1)} lbs`, hint: gStr };
    if (oz >= 16) return { label: `1 lb`, hint: gStr };
    return { label: `${Math.round(oz)} oz`, hint: gStr };
  }

  // ── Nuts & seeds ─────────────────────────────────────────────
  if (d.includes("almond") || d.includes("cashew") || d.includes("walnut") ||
      d.includes("pecan") || d.includes("pistachio") || d.includes("macadamia") ||
      d.includes("nut") || d.includes("seed") || d.includes("peanut")) {
    if (g <= 15) return { label: formatTbsp(g / 7), hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }

  // ── Dry grains ───────────────────────────────────────────────
  if (d.includes("oat") || d.includes("quinoa") || d.includes("barley") || d.includes("millet")) {
    return { label: formatCups(g / 90), hint: gStr };
  }
  if (d.includes("rice") || d.includes("farro") || d.includes("wheat berry")) {
    return { label: formatCups(g / 185), hint: gStr };
  }
  if (d.includes("flour") || d.includes("cornmeal") || d.includes("semolina")) {
    return { label: formatCups(g / 125), hint: gStr };
  }

  // ── Legumes & beans ──────────────────────────────────────────
  if (d.includes("lentil") || d.includes("chickpea") || d.includes("bean") ||
      d.includes("pea") || d.includes("soybean")) {
    return { label: formatCups(g / 200), hint: gStr };
  }

  // ── Cheese ───────────────────────────────────────────────────
  if (d.includes("cheese") || d.includes("parmesan") || d.includes("mozzarella") ||
      d.includes("cheddar") || d.includes("feta")) {
    if (g <= 15) return { label: formatTbsp(g / 5), hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }

  // ── Spices & seasonings ──────────────────────────────────────
  if (d.includes("spice") || d.includes("cumin") || d.includes("paprika") ||
      d.includes("cinnamon") || d.includes("garlic powder") || d.includes("onion powder")) {
    return { label: `${Math.round(g / 3)} tsp`, hint: gStr };
  }

  // ── Countable produce (sold by the piece) ───────────────────
  // Returns "N item(s) (Xg)"
  type ProduceSpec = { terms: string[]; gEach: number; singular: string; plural?: string };
  const countableProduce: ProduceSpec[] = [
    { terms: ["banana"], gEach: 118, singular: "banana" },
    { terms: ["apple"], gEach: 182, singular: "apple" },
    { terms: ["orange"], gEach: 131, singular: "orange" },
    { terms: ["lemon"], gEach: 58, singular: "lemon" },
    { terms: ["lime"], gEach: 67, singular: "lime" },
    { terms: ["avocado"], gEach: 150, singular: "avocado" },
    { terms: ["tomato"], gEach: 123, singular: "tomato" },
    { terms: ["bell pepper", "sweet pepper"], gEach: 119, singular: "bell pepper" },
    { terms: ["jalapeño", "jalapeno"], gEach: 14, singular: "jalapeño" },
    { terms: ["onion"], gEach: 110, singular: "onion" },
    { terms: ["shallot"], gEach: 40, singular: "shallot" },
    { terms: ["sweet potato"], gEach: 130, singular: "sweet potato" },
    { terms: ["potato"], gEach: 150, singular: "potato" },
    { terms: ["zucchini", "courgette"], gEach: 196, singular: "zucchini" },
    { terms: ["cucumber"], gEach: 200, singular: "cucumber" },
    { terms: ["carrot"], gEach: 61, singular: "carrot" },
    { terms: ["celery stalk", "celery rib"], gEach: 40, singular: "celery stalk" },
    { terms: ["garlic clove", "garlic, raw"], gEach: 3, singular: "clove garlic" },
    { terms: ["garlic"], gEach: 60, singular: "head garlic" },
    { terms: ["mango"], gEach: 200, singular: "mango" },
    { terms: ["peach"], gEach: 150, singular: "peach" },
    { terms: ["pear"], gEach: 178, singular: "pear" },
    { terms: ["kiwi"], gEach: 69, singular: "kiwi" },
    { terms: ["grapefruit"], gEach: 246, singular: "grapefruit" },
  ];
  for (const spec of countableProduce) {
    if (spec.terms.some((t) => d.includes(t))) {
      const count = Math.max(1, Math.round(g / spec.gEach));
      const name = count === 1 ? spec.singular : (spec.plural ?? `${spec.singular}s`);
      return { label: `${count} ${name}`, hint: gStr };
    }
  }

  // ── Bulk produce (berries, greens, etc.) ─────────────────────
  if (d.includes("blueberr")) {
    // 1 pint ≈ 255g; 1 cup ≈ 145g
    if (g >= 230) return { label: `1 pint`, hint: gStr };
    return { label: formatCups(g / 145), hint: gStr };
  }
  if (d.includes("strawberr")) {
    if (g >= 300) return { label: `1 pint`, hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("raspberr") || d.includes("blackberr")) {
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("grape") && !d.includes("grapefruit")) {
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("cherry") || d.includes("cherri")) {
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("spinach") || d.includes("kale") || d.includes("arugula") ||
      d.includes("lettuce") || d.includes("chard") || d.includes("mixed green")) {
    // 1 cup loosely packed ≈ 30g
    if (g >= 85) return { label: `${Math.round(g / 28.35)} oz bag`, hint: gStr };
    return { label: formatCups(g / 30), hint: gStr };
  }
  if (d.includes("broccoli")) {
    if (g >= 250) return { label: `1 head`, hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("cauliflower")) {
    if (g >= 500) return { label: `1 head`, hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("asparagus")) {
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("mushroom")) {
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("celery")) {
    // bunch ≈ 400g
    if (g >= 350) return { label: `1 bunch`, hint: gStr };
    const stalks = Math.max(1, Math.round(g / 40));
    return { label: `${stalks} stalk${stalks !== 1 ? "s" : ""}`, hint: gStr };
  }
  if (d.includes("ginger")) {
    if (g <= 10) return { label: `1-inch piece`, hint: gStr };
    return { label: `${Math.round(g / 10)}-inch piece`, hint: gStr };
  }

  // ── Generic produce / everything else ───────────────────────
  const oz = g / 28.35;
  if (g < 30)  return { label: `${Math.round(g)}g`, hint: "" };
  if (oz >= 32) return { label: `${(oz / 16).toFixed(1)} lbs`, hint: gStr };
  return { label: `${Math.round(oz)} oz`, hint: gStr };
}

/**
 * Strip common USDA boilerplate suffixes to produce a cleaner display name.
 * e.g. "Bananas, raw" → "Bananas"  |  "Chicken breast, cooked" → "Chicken breast"
 */
function friendlyName(description: string): string {
  return description
    .replace(/,\s*(raw|cooked|fresh|dried|canned|frozen|whole|uncooked|unprepared|boiled|steamed|roasted|baked|grilled|fried|broiled|drained|rinsed|sliced|chopped|diced|minced|peeled|pitted|shelled|unsalted|salted|plain|regular|reduced.fat|low.fat|nonfat|fat.free|without\s+skin|with\s+skin|boneless|bone.in|ground)\b.*/gi, "")
    .trim();
}

interface Props {
  plan: MealPlan;
}

export default function ShoppingList({ plan }: Props) {
  const items = buildShoppingList(plan);

  return (
    <View>
      <Text className="text-xl font-bold text-gray-900 mb-1">{plan.name}</Text>
      <Text className="text-sm text-gray-500 mb-4">{items.length} ingredients</Text>

      {items.map((item) => {
        const { label, hint } = gramsToConsumer(item.total_g, item.description);
        return (
          <View
            key={item.fdc_id}
            className="flex-row items-start justify-between py-3 border-b border-gray-100"
          >
            <View className="flex-1 mr-4">
              <Text className="text-sm font-medium text-gray-900">{friendlyName(item.description)}</Text>
              <Text className="text-xs text-gray-400 mt-0.5">
                {item.meals.join(", ")}
              </Text>
            </View>
            <View className="items-end">
              <Text className="text-sm font-semibold text-gray-800">{label}</Text>
              {hint ? (
                <Text className="text-xs text-gray-400">{hint}</Text>
              ) : null}
            </View>
          </View>
        );
      })}
    </View>
  );
}
