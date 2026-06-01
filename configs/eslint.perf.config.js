// Performance-focused ESLint config for the mobile-perf-audit.
//
// Used by scripts/static_scan.py via:
//   npx eslint --config configs/eslint.perf.config.js --format json <workspace>
//
// Rules are scoped to actual perf concerns. General code-quality rules are
// out of scope for this pipeline.
//
// Required plugins (must be present in the workspace's node_modules):
//   - eslint-plugin-react
//   - eslint-plugin-react-hooks
//   - eslint-plugin-react-perf
//   - eslint-plugin-react-native
//
// If a plugin isn't installed in the workspace, static_scan.py installs it
// inside the workspace (yarn add / npm install) as a fail-soft.

/** @type {import('eslint').Linter.FlatConfig[]} */
module.exports = [
  {
    files: ["**/*.{js,jsx,ts,tsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parser: require("@typescript-eslint/parser"),
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      react: require("eslint-plugin-react"),
      "react-hooks": require("eslint-plugin-react-hooks"),
      "react-perf": require("eslint-plugin-react-perf"),
      "react-native": require("eslint-plugin-react-native"),
    },
    settings: {
      react: { version: "detect" },
    },
    rules: {
      // ── react-hooks: the most consequential perf rule set in RN apps ──
      // Bad dep arrays cause infinite re-renders / stale closures /
      // missed memoization. Treat as errors.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",

      // ── react-perf: catches the inline-prop class of issues ──
      // Each of these creates a new identity on every render → defeats
      // React.memo and PureComponent.
      "react-perf/jsx-no-new-object-as-prop": "error",
      "react-perf/jsx-no-new-array-as-prop": "error",
      "react-perf/jsx-no-new-function-as-prop": "error",
      "react-perf/jsx-no-jsx-as-prop": "warn",

      // ── react-native: RN-specific antipatterns ──
      "react-native/no-inline-styles": "warn",         // perf-relevant; new style object each render
      "react-native/no-raw-text": "off",               // a11y, not perf — disabled
      "react-native/no-unused-styles": "warn",         // dead StyleSheets ship bytes

      // ── react: keys are perf-critical for list reconciliation ──
      "react/jsx-key": "error",
      "react/no-array-index-key": "warn",              // index keys re-render unnecessarily on insertions
    },
  },

  // Tests / generated / build outputs — out of perf scope
  {
    ignores: [
      "**/node_modules/**",
      "**/__tests__/**",
      "**/*.test.{js,jsx,ts,tsx}",
      "**/*.spec.{js,jsx,ts,tsx}",
      "**/dist/**",
      "**/build/**",
      "**/.expo/**",
      "**/ios/**",
      "**/android/**",
      "**/coverage/**",
    ],
  },
];
