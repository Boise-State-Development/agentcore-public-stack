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
import * as path from 'path';
import { fromProjectRoot } from '../testing/project-root';
import { readSourceTree, SOURCE_TREE_SCAN_TIMEOUT_MS } from '../testing/source-tree';

describe('tailwind theme-import hygiene', () => {
  // Scans all of src, so it takes the scan timeout, not the 5 s default
  // (see source-tree.ts).
  it('only styles/theme.css may @import "tailwindcss"; every other stylesheet must @reference the shared theme', () => {
    // The single allowed owner of `@import "tailwindcss"`, relative to src/.
    const ALLOWED_TAILWIND_IMPORTER = path.normalize('styles/theme.css');

    const srcDir = fromProjectRoot('src');

    // Both standalone `.css` stylesheets AND `.ts` components with an inline
    // `styles:` block can carry `@import "tailwindcss"` — an inline import
    // regenerates the default theme just as a `.css` one does (this is how
    // the user-menu hover regressed to white). Scan both. styles/generated
    // is scanned too: a generator that emitted the import would break the
    // theme just the same.
    const styleFiles = readSourceTree(srcDir, {
      extensions: ['.css', '.ts'],
      skipFile: (name) => name.endsWith('.spec.ts'),
    });

    // Matches a real `@import "tailwindcss"` / `@import 'tailwindcss'`
    // statement, not a mention inside a comment (comments are stripped
    // first).
    const tailwindImportRegex = /@import\s+["']tailwindcss["']/;
    const blockCommentRegex = /\/\*[\s\S]*?\*\//g;

    const violations: string[] = [];

    for (const { relativePath, content } of styleFiles) {
      // Almost no file names tailwindcss at all; skip the comment strip for them.
      if (!content.includes('tailwindcss')) continue;
      const withoutComments = content.replace(blockCommentRegex, '');
      if (tailwindImportRegex.test(withoutComments) && relativePath !== ALLOWED_TAILWIND_IMPORTER) {
        violations.push(relativePath);
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
  }, SOURCE_TREE_SCAN_TIMEOUT_MS);
});
