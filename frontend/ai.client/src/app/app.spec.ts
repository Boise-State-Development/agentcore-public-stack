import { describe, it, expect } from 'vitest';

// These two load the root component through a runtime dynamic `import()`, so
// vite's module runner transforms the App dependency graph on demand rather
// than at build time. That measures ~4s on a developer machine — against
// vitest's 5000ms default this leaves ~20% headroom, and any parallel load
// (a busy CI runner, another suite on the same box) pushes it over, failing
// as "Test timed out in 5000ms". The import is deliberately dynamic: this
// spec having no static Angular imports is what originally surfaced the
// JIT/PlatformLocation ordering bug that `src/test-setup.ts` guards against,
// so raise the budget rather than hoisting the import to a static one.
const IMPORT_TIMEOUT_MS = 30_000;

describe('App', () => {
  it(
    'should export App component class',
    async () => {
      const { App } = await import('./app');
      expect(App).toBeDefined();
    },
    IMPORT_TIMEOUT_MS,
  );

  it(
    'should have newChat as a method',
    async () => {
      const { App } = await import('./app');
      expect(App.prototype.newChat).toBeDefined();
    },
    IMPORT_TIMEOUT_MS,
  );
});
