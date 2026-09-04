// @vitest-environment node
//
// Tailwind theme-import guard — prevents the "white sidebar/toolbar on a
// pink page" class of regression.
//
// In Angular, every component stylesheet is compiled independently. A
// stylesheet that does its own `@import "tailwindcss";` regenerates
// Tailwind's theme + utilities from Tailwind's DEFAULT palette, with no
// knowledge of the brand/surface overrides in styles/generated/*.css — so
// that component silently ignores `brand.config.ts`'s `surfaces`. At
// Default_Surfaces the override is byte-identical to Tailwind's stock
// neutrals, so the divergence is invisible until someone sets a genuinely
// non-default surface (see the brand-surface-colors-not-applied bugfix).
//
// The rule: exactly ONE stylesheet may `@import "tailwindcss"` — the
// shared entry `src/styles/theme.css`, which also pulls in the brand /
// surface overrides. Every other stylesheet must `@reference` that shared
// entry instead, so its `dark:` / theme-var / `@apply` usage resolves
// against the SAME overridden theme without re-emitting a stock-palette
// copy of Tailwind.
import { describe, expect, it } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

describe('tailwind theme-import hygiene', () => {
  it('only styles/theme.css may @import "tailwindcss"; every other stylesheet must @reference the shared theme', () => {
    // The single allowed owner of `@import "tailwindcss"`, relative to src/.
    const ALLOWED_TAILWIND_IMPORTER = path.normalize('styles/theme.css');

    const srcDir = path.resolve(__dirname, '..');
    const styleFiles: string[] = [];

    // Both standalone `.css` stylesheets AND `.ts` components with an inline
    // `styles:` block can carry `@import "tailwindcss"` — an inline import
    // regenerates the default theme just as a `.css` one does (this is how
    // the user-menu hover regressed to white). Scan both.
    function walkDir(dir: string): void {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const fullPath = path.join(dir, entry.name);
        if (/node_modules/.test(fullPath)) continue;
        if (entry.isDirectory()) {
          walkDir(fullPath);
        } else if (entry.name.endsWith('.css') || (entry.name.endsWith('.ts') && !entry.name.endsWith('.spec.ts'))) {
          styleFiles.push(fullPath);
        }
      }
    }

    walkDir(srcDir);

    // Matches a real `@import "tailwindcss"` / `@import 'tailwindcss'`
    // statement, not a mention inside a comment (comments are stripped
    // first).
    const tailwindImportRegex = /@import\s+["']tailwindcss["']/;
    const blockCommentRegex = /\/\*[\s\S]*?\*\//g;

    const violations: string[] = [];

    for (const file of styleFiles) {
      const relative = path.normalize(path.relative(srcDir, file));
      const withoutComments = fs.readFileSync(file, 'utf-8').replace(blockCommentRegex, '');
      if (tailwindImportRegex.test(withoutComments) && relative !== ALLOWED_TAILWIND_IMPORTER) {
        violations.push(relative);
      }
    }

    const message = [
      'These stylesheets import Tailwind directly, which regenerates utilities from the DEFAULT',
      'theme and silently ignores the brand/surface overrides (white surfaces on a themed page):',
      '',
      ...violations.map((v) => `  ${v}`),
      '',
      'Fix: replace `@import "tailwindcss";` with `@reference "<relative>/styles/theme.css";`',
      'so the component resolves against the shared, overridden theme. Only styles/theme.css may',
      'own the `@import "tailwindcss"` (see its header).',
    ].join('\n');

    expect(violations, message).toEqual([]);
  });
});
