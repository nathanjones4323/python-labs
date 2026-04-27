export function formatCups(cups: number): string {
  if (cups <= 0.15) return "1 tbsp";
  if (cups <= 0.3)  return "¼ cup";
  if (cups <= 0.6)  return "½ cup";
  if (cups <= 0.9)  return "¾ cup";
  const whole = Math.round(cups);
  return `${whole} cup${whole !== 1 ? "s" : ""}`;
}

export function formatTbsp(tbsp: number): string {
  const rounded = Math.round(tbsp * 2) / 2;
  if (rounded < 1) return "1 tbsp";
  if (rounded === 0.5) return "½ tbsp";
  const whole = Math.floor(rounded);
  const half = rounded % 1 > 0;
  return half ? `${whole}½ tbsp` : `${whole} tbsp`;
}

export function gramsToConsumer(g: number, description: string): { label: string; hint: string } {
  const d = description.toLowerCase();
  const gStr = `${Math.round(g)}g`;

  if (d.includes("oil") || d.includes("ghee") ||
      (d.includes("butter") && !d.includes("peanut") && !d.includes("nut"))) {
    if (g <= 5) return { label: "1 tsp", hint: gStr };
    if (g <= 60) return { label: formatTbsp(g / 14), hint: gStr };
    return { label: formatCups(g / 225), hint: gStr };
  }
  if (d.includes("honey") || d.includes("maple") || d.includes("syrup") || d.includes("agave")) {
    if (g <= 5) return { label: "1 tsp", hint: gStr };
    return { label: formatTbsp(g / 21), hint: gStr };
  }
  if ((d.includes("milk") || d.includes("cream") || d.includes("kefir")) && !d.includes("powder")) {
    return { label: formatCups(g / 240), hint: gStr };
  }
  if (d.includes("yogurt") || d.includes("sour cream") || d.includes("cottage")) {
    return { label: formatCups(g / 245), hint: gStr };
  }
  if (d.includes("egg") && !d.includes("eggplant")) {
    const eggs = Math.max(1, Math.round(g / 50));
    return { label: `${eggs} egg${eggs !== 1 ? "s" : ""}`, hint: gStr };
  }
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
  if (d.includes("almond") || d.includes("cashew") || d.includes("walnut") ||
      d.includes("pecan") || d.includes("pistachio") || d.includes("macadamia") ||
      d.includes("nut") || d.includes("seed") || d.includes("peanut")) {
    if (g <= 15) return { label: formatTbsp(g / 7), hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("oat") || d.includes("quinoa") || d.includes("barley") || d.includes("millet")) {
    return { label: formatCups(g / 90), hint: gStr };
  }
  if (d.includes("rice") || d.includes("farro") || d.includes("wheat berry")) {
    return { label: formatCups(g / 185), hint: gStr };
  }
  if (d.includes("flour") || d.includes("cornmeal") || d.includes("semolina")) {
    return { label: formatCups(g / 125), hint: gStr };
  }
  if (d.includes("lentil") || d.includes("chickpea") || d.includes("bean") || d.includes("pea") || d.includes("soybean")) {
    return { label: formatCups(g / 200), hint: gStr };
  }
  if (d.includes("cheese") || d.includes("parmesan") || d.includes("mozzarella") ||
      d.includes("cheddar") || d.includes("feta")) {
    if (g <= 15) return { label: formatTbsp(g / 5), hint: gStr };
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("spice") || d.includes("cumin") || d.includes("paprika") ||
      d.includes("cinnamon") || d.includes("garlic powder") || d.includes("onion powder")) {
    return { label: `${Math.round(g / 3)} tsp`, hint: gStr };
  }

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

  if (d.includes("blueberr")) {
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
  if (d.includes("asparagus") || d.includes("mushroom")) {
    return { label: `${Math.round(g / 28.35)} oz`, hint: gStr };
  }
  if (d.includes("celery")) {
    if (g >= 350) return { label: `1 bunch`, hint: gStr };
    const stalks = Math.max(1, Math.round(g / 40));
    return { label: `${stalks} stalk${stalks !== 1 ? "s" : ""}`, hint: gStr };
  }
  if (d.includes("ginger")) {
    if (g <= 10) return { label: `1-inch piece`, hint: gStr };
    return { label: `${Math.round(g / 10)}-inch piece`, hint: gStr };
  }

  const oz = g / 28.35;
  if (g < 30)  return { label: `${Math.round(g)}g`, hint: "" };
  if (oz >= 32) return { label: `${(oz / 16).toFixed(1)} lbs`, hint: gStr };
  return { label: `${Math.round(oz)} oz`, hint: gStr };
}

export function friendlyName(description: string): string {
  return description
    .replace(/,\s*(raw|cooked|fresh|dried|canned|frozen|whole|uncooked|unprepared|boiled|steamed|roasted|baked|grilled|fried|broiled|drained|rinsed|sliced|chopped|diced|minced|peeled|pitted|shelled|unsalted|salted|plain|regular|reduced.fat|low.fat|nonfat|fat.free|without\s+skin|with\s+skin|boneless|bone.in|ground)\b.*/gi, "")
    .trim();
}
