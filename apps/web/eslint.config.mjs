// @ts-check
import js from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";

/**
 * The two boundary rules below are the frontend half of goal G8 in
 * docs/01-ARCHITECTURE.md: the isometric island must stay a projection of
 * domain state, deletable without touching anything else.
 *
 * They are configured in Phase 0, before `world/` contains any code, because
 * a rule added after the fact only documents violations that already exist.
 */
const PHASER_QUARANTINE = {
  patterns: [
    {
      group: ["phaser", "phaser/*"],
      message:
        "Phaser may only be imported inside world/phaser/. Everything else talks to the " +
        "projector's plain WorldState (docs/01-ARCHITECTURE.md section 6).",
    },
    {
      group: ["**/world/phaser/*", "@/world/phaser/*"],
      message:
        "world/phaser/ is private to the visual layer. Import from world/projector or " +
        "world/types instead, so deleting the scene cannot break the app.",
    },
  ],
};

const PROJECTOR_PURITY = {
  patterns: [
    {
      group: ["phaser", "phaser/*", "react", "react-dom", "next/*"],
      message:
        "world/projector must stay pure: plain data in, plain WorldState out. No React, " +
        "no Phaser, no framework - that is what makes it unit-testable without a canvas.",
    },
    {
      group: ["@/lib/api/*", "**/lib/api/*"],
      message:
        "world/projector receives DomainState as an argument; it must not fetch anything.",
    },
  ],
};

export default [
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts", "lib/api/generated/**"],
  },
  js.configs.recommended,
  {
    files: ["**/*.{ts,tsx,mts}"],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: "latest", sourceType: "module" },
      globals: { console: "readonly", process: "readonly", fetch: "readonly" },
    },
    plugins: { "@typescript-eslint": tseslint },
    rules: {
      ...tseslint.configs.recommended.rules,
      // tsc already resolves every identifier, including DOM and React types
      // that eslint has no way to know about. Leaving no-undef on produces
      // false positives on `RequestInit`, `URL` and `React.ReactNode`, and
      // typescript-eslint documents turning it off for TS files.
      "no-undef": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/consistent-type-imports": "error",
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
  {
    // Everything except the visual layer itself.
    files: ["**/*.{ts,tsx}"],
    ignores: ["world/**"],
    rules: { "no-restricted-imports": ["error", PHASER_QUARANTINE] },
  },
  {
    files: ["world/projector/**/*.ts"],
    rules: { "no-restricted-imports": ["error", PROJECTOR_PURITY] },
  },
  {
    files: ["**/*.test.{ts,tsx}", "**/*.config.{ts,mts}"],
    rules: { "no-console": "off" },
  },
];
