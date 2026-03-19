// ESLint v9 flat config for Decky Cloud Reader
// Uses @typescript-eslint for TS parsing + eslint-config-prettier to avoid
// formatting conflicts (Prettier handles all style rules).
import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import prettierConfig from "eslint-config-prettier";

export default [
  // Global ignores — build output, dependencies, config files
  {
    ignores: ["dist/", "node_modules/", "rollup.config.js"],
  },

  // TypeScript source files
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      // Recommended subset — avoid rules that conflict with the Decky/Steam environment
      ...tsPlugin.configs.recommended.rules,
      // Allow explicit `any` — Decky/Steam internals often need it
      "@typescript-eslint/no-explicit-any": "off",
      // Allow unused vars prefixed with _ (common convention)
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },

  // Prettier compat — disables all formatting rules that conflict
  prettierConfig,
];
