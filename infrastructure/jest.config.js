module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.tsx?$': ['ts-jest', {
      // Transpile-only: skip per-worker type-checking (each jest worker
      // otherwise re-type-checks the whole project with no shared cache, the
      // dominant cost of this suite). Type safety is enforced once by the
      // `tsc --noEmit` step in the CI job (.github/workflows/tests.yml) and by
      // `npm run build` locally. Safe here: no `const enum` in the tree.
      isolatedModules: true,
      diagnostics: {
        ignoreCodes: [151002],
      },
    }],
  },
};
