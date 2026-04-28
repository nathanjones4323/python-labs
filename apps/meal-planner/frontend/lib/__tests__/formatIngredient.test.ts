import {
  formatCups,
  formatTbsp,
  gramsToConsumer,
  friendlyName,
} from "../formatIngredient";

// ── formatCups ────────────────────────────────────────────────────────────────

describe("formatCups", () => {
  it("returns 1 tbsp for very small amounts", () => {
    expect(formatCups(0.1)).toBe("1 tbsp");
    expect(formatCups(0.15)).toBe("1 tbsp");
  });

  it("returns ¼ cup", () => {
    expect(formatCups(0.25)).toBe("¼ cup");
    expect(formatCups(0.3)).toBe("¼ cup");
  });

  it("returns ½ cup", () => {
    expect(formatCups(0.5)).toBe("½ cup");
    expect(formatCups(0.6)).toBe("½ cup");
  });

  it("returns ¾ cup", () => {
    expect(formatCups(0.75)).toBe("¾ cup");
    expect(formatCups(0.9)).toBe("¾ cup");
  });

  it("returns 1 cup", () => {
    expect(formatCups(1.0)).toBe("1 cup");
    expect(formatCups(1.1)).toBe("1 cup");
  });

  it("returns 1¼ cups", () => {
    expect(formatCups(1.25)).toBe("1¼ cups");
    expect(formatCups(1.4)).toBe("1¼ cups");
  });

  it("returns 1½ cups", () => {
    expect(formatCups(1.5)).toBe("1½ cups");
    expect(formatCups(1.6)).toBe("1½ cups");
  });

  it("returns 1¾ cups", () => {
    expect(formatCups(1.75)).toBe("1¾ cups");
    expect(formatCups(1.9)).toBe("1¾ cups");
  });

  it("returns 2 cups", () => {
    expect(formatCups(2.0)).toBe("2 cups");
    expect(formatCups(2.1)).toBe("2 cups");
  });

  it("returns 2¼ cups", () => {
    expect(formatCups(2.25)).toBe("2¼ cups");
    expect(formatCups(2.4)).toBe("2¼ cups");
  });

  it("returns 2½ cups", () => {
    expect(formatCups(2.5)).toBe("2½ cups");
    expect(formatCups(2.6)).toBe("2½ cups");
  });

  it("returns 2¾ cups", () => {
    expect(formatCups(2.75)).toBe("2¾ cups");
    expect(formatCups(2.9)).toBe("2¾ cups");
  });

  it("returns whole cups for large amounts", () => {
    expect(formatCups(3.0)).toBe("3 cups");
    expect(formatCups(4.0)).toBe("4 cups");
  });
});

// ── formatTbsp ────────────────────────────────────────────────────────────────

describe("formatTbsp", () => {
  it("returns ½ tbsp for 0.5 tbsp (regression: was unreachable dead branch)", () => {
    expect(formatTbsp(0.5)).toBe("½ tbsp");
  });

  it("returns 1 tbsp for very small amounts", () => {
    // 0.1 * 2 = 0.2 → Math.round → 0 → falls through to "1 tbsp"
    expect(formatTbsp(0.1)).toBe("1 tbsp");
    expect(formatTbsp(0.2)).toBe("1 tbsp");
  });

  it("returns 1 tbsp for 1 tbsp", () => {
    expect(formatTbsp(1)).toBe("1 tbsp");
  });

  it("returns whole tablespoons", () => {
    expect(formatTbsp(2)).toBe("2 tbsp");
    expect(formatTbsp(3)).toBe("3 tbsp");
  });

  it("returns half tablespoons", () => {
    expect(formatTbsp(1.5)).toBe("1½ tbsp");
    expect(formatTbsp(2.5)).toBe("2½ tbsp");
  });
});

// ── gramsToConsumer ───────────────────────────────────────────────────────────

describe("gramsToConsumer", () => {
  describe("pea false-match regression", () => {
    it("pear is counted by unit, not treated as a legume", () => {
      expect(gramsToConsumer(178, "pear").label).toBe("1 pear");
      expect(gramsToConsumer(356, "pear, raw").label).toBe("2 pears");
    });

    it("peach is counted by unit, not treated as a legume", () => {
      expect(gramsToConsumer(150, "peach").label).toBe("1 peach");
      expect(gramsToConsumer(300, "peach, fresh").label).toBe("2 peaches");
    });

    it("peas and pea are treated as legumes", () => {
      const label = gramsToConsumer(200, "green peas").label;
      expect(label).toMatch(/cup/);
    });

    it("snap peas are treated as legumes", () => {
      const label = gramsToConsumer(200, "snap peas").label;
      expect(label).toMatch(/cup/);
    });

    it("peanut is treated as a nut, not a legume", () => {
      // <= 15g → tbsp; > 15g → oz — neither is a legume cups result
      expect(gramsToConsumer(14, "peanut butter").label).toBe("2 tbsp");
      expect(gramsToConsumer(30, "peanut butter").label).toBe("1 oz");
    });
  });

  describe("oils and fats", () => {
    it("returns tsp for tiny amounts of oil", () => {
      expect(gramsToConsumer(5, "olive oil").label).toBe("1 tsp");
    });

    it("returns tbsp for moderate oil", () => {
      expect(gramsToConsumer(14, "olive oil").label).toBe("1 tbsp");
      expect(gramsToConsumer(28, "coconut oil").label).toBe("2 tbsp");
    });

    it("returns cups for large amounts of oil", () => {
      expect(gramsToConsumer(225, "olive oil").label).toBe("1 cup");
    });

    it("returns tbsp for butter", () => {
      expect(gramsToConsumer(14, "butter, unsalted").label).toBe("1 tbsp");
    });

    it("does not classify peanut butter as a fat", () => {
      const label = gramsToConsumer(32, "peanut butter").label;
      expect(label).not.toMatch(/tbsp/);
    });
  });

  describe("dairy", () => {
    it("milk returns cups", () => {
      expect(gramsToConsumer(240, "whole milk").label).toBe("1 cup");
    });

    it("sour cream goes through the yogurt path, not the milk/cream path", () => {
      expect(gramsToConsumer(245, "sour cream").label).toBe("1 cup");
    });

    it("cream cheese goes through the cheese path, not the milk/cream path", () => {
      const label = gramsToConsumer(226, "cream cheese").label;
      expect(label).toBe("8 oz");
      expect(label).not.toMatch(/cup/);
    });

    it("yogurt returns cups", () => {
      expect(gramsToConsumer(245, "plain yogurt").label).toBe("1 cup");
    });
  });

  describe("eggs", () => {
    it("returns egg count", () => {
      expect(gramsToConsumer(50, "egg, large").label).toBe("1 egg");
      expect(gramsToConsumer(100, "egg, large").label).toBe("2 eggs");
    });

    it("eggplant does not match egg logic", () => {
      const label = gramsToConsumer(100, "eggplant").label;
      expect(label).not.toMatch(/egg/);
    });
  });

  describe("meat and fish", () => {
    it("returns oz for small portions", () => {
      expect(gramsToConsumer(113, "chicken breast").label).toBe("4 oz");
    });

    it("returns at least 1 oz for very small portions (no '0 oz')", () => {
      expect(gramsToConsumer(14, "salmon fillet").label).toBe("1 oz");
    });

    it("returns 1 lb for 16 oz", () => {
      expect(gramsToConsumer(454, "ground beef").label).toBe("1 lb");
    });

    it("returns lbs for large amounts", () => {
      // 907g = 31.99 oz (just under 32 oz threshold); use 910g = 32.1 oz
      const label = gramsToConsumer(910, "salmon").label;
      expect(label).toMatch(/lbs$/);
    });
  });

  describe("grains", () => {
    it("oats return cups", () => {
      expect(gramsToConsumer(90, "rolled oats").label).toBe("1 cup");
    });

    it("rice returns cups", () => {
      expect(gramsToConsumer(185, "white rice, uncooked").label).toBe("1 cup");
    });

    it("quinoa returns cups", () => {
      expect(gramsToConsumer(90, "quinoa").label).toBe("1 cup");
    });
  });

  describe("countable produce", () => {
    it("single banana", () => {
      expect(gramsToConsumer(118, "banana").label).toBe("1 banana");
    });

    it("multiple bananas", () => {
      expect(gramsToConsumer(236, "banana").label).toBe("2 bananas");
    });

    it("avocado", () => {
      expect(gramsToConsumer(150, "avocado, raw").label).toBe("1 avocado");
    });

    it("bell pepper", () => {
      expect(gramsToConsumer(119, "bell pepper, red").label).toBe("1 bell pepper");
    });

    it("garlic (whole head vs cloves)", () => {
      expect(gramsToConsumer(60, "garlic").label).toBe("1 head garlic");
      expect(gramsToConsumer(120, "garlic").label).toBe("2 heads garlic");
      expect(gramsToConsumer(3, "garlic, raw").label).toBe("1 clove garlic");
      expect(gramsToConsumer(9, "garlic, raw").label).toBe("3 cloves garlic");
    });

    it("sweet potato before plain potato", () => {
      expect(gramsToConsumer(130, "sweet potato").label).toBe("1 sweet potato");
    });
  });

  describe("berries", () => {
    it("blueberries under pint threshold return cups", () => {
      expect(gramsToConsumer(145, "blueberries").label).toBe("1 cup");
    });

    it("blueberries at pint threshold return pint", () => {
      expect(gramsToConsumer(230, "blueberries, fresh").label).toBe("1 pint");
    });

    it("strawberries at pint threshold return pint", () => {
      expect(gramsToConsumer(300, "strawberries").label).toBe("1 pint");
    });
  });

  describe("leafy greens", () => {
    it("small amounts return cups", () => {
      expect(gramsToConsumer(30, "spinach, raw").label).toBe("1 cup");
    });

    it("bag-size amounts return oz bag", () => {
      expect(gramsToConsumer(142, "baby spinach").label).toBe("5 oz bag");
    });
  });

  describe("fallback", () => {
    it("tiny unknowns stay in grams", () => {
      // "tofu" matches no category — falls through to the g < 30 → grams path
      expect(gramsToConsumer(10, "tofu").label).toBe("10g");
    });

    it("larger unknowns convert to oz", () => {
      const label = gramsToConsumer(85, "tofu").label;
      expect(label).toMatch(/oz$/);
    });
  });

  describe("hint field", () => {
    it("always returns the raw gram amount as hint (except tiny unknowns)", () => {
      expect(gramsToConsumer(240, "whole milk").hint).toBe("240g");
      expect(gramsToConsumer(150, "banana").hint).toBe("150g");
    });

    it("returns empty hint for tiny unknown ingredients", () => {
      expect(gramsToConsumer(10, "tofu").hint).toBe("");
    });
  });
});

// ── friendlyName ──────────────────────────────────────────────────────────────

describe("friendlyName", () => {
  it("strips preparation qualifiers after a comma", () => {
    expect(friendlyName("chicken breast, raw")).toBe("chicken breast");
    expect(friendlyName("spinach, cooked")).toBe("spinach");
    expect(friendlyName("salmon, baked")).toBe("salmon");
  });

  it("strips multiple qualifier words", () => {
    expect(friendlyName("beef, ground, lean")).toBe("beef");
    expect(friendlyName("almonds, unsalted, roasted")).toBe("almonds");
  });

  it("handles fat/skin qualifiers", () => {
    expect(friendlyName("chicken, without skin")).toBe("chicken");
    expect(friendlyName("chicken thigh, bone-in")).toBe("chicken thigh");
  });

  it("leaves names with no qualifiers unchanged", () => {
    expect(friendlyName("banana")).toBe("banana");
    expect(friendlyName("olive oil")).toBe("olive oil");
  });

  it("trims surrounding whitespace", () => {
    expect(friendlyName("  apple  ")).toBe("apple");
  });
});
