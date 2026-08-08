// Flat ESLint config for the renderer and the dev launcher.
//
// TypeScript's own strictness (see tsconfig.json) already covers correctness, so this config
// only carries what the type checker cannot see: the rules of hooks, the constraints Fast
// Refresh places on a module's exports, and a ban on the two escape hatches that would let a
// type error through silently.

import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    // Rust output, the bundle, and the generated Tauri ACL are not ours to lint.
    ignores: ['dist/**', 'src-tauri/**', 'node_modules/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,

      // Fast Refresh can only preserve state for a module that exports components and nothing
      // else. `allowConstantExport` covers the shadcn convention of exporting a `cva` variant
      // object alongside its component.
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      // An unused parameter is usually a signature that drifted. The underscore prefix is the
      // documented way to say "required by the signature, deliberately unused".
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],

      // `any` and `@ts-ignore` are how a DTO mismatch between `src/lib/api/types.ts` and
      // `app/schemas/` gets past the compiler and turns into a blank screen at runtime.
      // `docs/CONTRACTS.md` §18 makes that mirror binding, so neither is available here.
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/ban-ts-comment': [
        'error',
        { 'ts-expect-error': 'allow-with-description', 'ts-ignore': true, 'ts-nocheck': true },
      ],
    },
  },
  {
    files: ['scripts/**/*.mjs', 'vite.config.ts', 'eslint.config.js'],
    languageOptions: {
      globals: globals.node,
    },
    rules: {
      // The launcher's whole job is to narrate what it is doing to the terminal.
      'no-console': 'off',
    },
  },
);
