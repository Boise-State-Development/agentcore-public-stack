// @vitest-environment node
//
// Color token hygiene guard — enforce the Tailwind palette migration ratchet.
//
// This spec scans src/app/**/*.{ts,html} for raw Tailwind palette utilities and
// asserts they appear only in explicitly pending palettes. Neutral colors
// (gray, slate, zinc, neutral, stone, white, black) are always allowed per
// .kiro/steering/tailwind-colors.md — not because they're unthemed (as of the
// surfaces feature, `gray-*` and `white` ARE rebrandable, remapped at build
// time from `Brand_Surface` in brand.config.ts — see
// src/branding/README.md section 4), but because they don't need mapping to
// a *named* token the way brand/status/category colors do: `gray-*`/`white`
// already read through the themed ramp directly.
//
// A failure here means:
// 1. A raw palette utility (e.g., bg-red-600) crept into application code.
// 2. It belongs to a palette that has already been migrated to tokens.
// 3. Fix: Map the utility to the appropriate token (primary-*, state-*, vendor-*,
//    filetype-*, etc.) per .kiro/steering/tailwind-colors.md section 6.
// 4. If the color is one stop of a decorative two-color gradient, see
//    GRADIENT_EXCEPTION_FILES below. That is the only exemption.
//
// The ratchet works by maintaining explicit pending palettes. Every time Phase 3
// or another track clears a palette, move it from pending into banned. This guard
// prevents silent regression.
//
// The matcher is a pure function, findPaletteViolations(), with its own tests
// at the bottom of this file. It went years without reporting anything because
// the palette was a non-capturing group, so match[1] was the numeric step and
// never equalled a banned palette name. The self-tests exist so a broken
// matcher fails loudly instead of passing a dirty tree.

import { describe, expect, it } from 'vitest';
import * as path from 'path';
import { fromProjectRoot } from '../testing/project-root';
import { readSourceTree, SOURCE_TREE_SCAN_TIMEOUT_MS } from '../testing/source-tree';

// Banned palettes — these are fully migrated and must never appear.
const BANNED_PALETTES = [
  'red',
  'amber',
  'green',
  'emerald',
  'yellow',
  'rose',
  'cyan',
  'teal',
  'lime',
  'fuchsia',
  'pink',
  'violet',
  'orange',   // api-keys migration complete
  'indigo',   // dialog conventions complete
  'purple',   // admin pages complete
  'sky',      // all utilities complete
];

// Pending palettes — kept for reference, now empty. All raw palettes are banned.
const PENDING_PALETTES: string[] = [];

// Two-color decorative gradients are out of scope for the token system. A
// banned palette is allowed only as a gradient stop (from-/via-/to-), only on a
// line that also sets a gradient background (bg-linear-*, bg-gradient-*,
// bg-radial, bg-conic), and only in these files. Paths are relative to src/app.
// A new gradient elsewhere fails until it is added here on purpose, and an
// entry that no longer exempts anything fails too, so the list cannot go stale.
const GRADIENT_EXCEPTION_FILES = [
  'agents/detail/agent-detail.page.ts',            // hero: from-blue-700 to-sky-500, dark:to-sky-700
  'settings/pages/profile/profile-settings.page.ts', // avatar: from-blue-500 to-indigo-600
];

const GRADIENT_STOP_PREFIXES = new Set(['from', 'via', 'to']);
const GRADIENT_BACKGROUND = /(?<![\w-])bg-(?:linear|gradient|radial|conic)(?![\w])/;

// Pattern: (prefix)-(palette)-(step). The lookbehind keeps "into-red-5" or
// "goto-sky-1" from reading as a utility, while still matching after a variant
// ("dark:hover:bg-red-600") or the important modifier ("!bg-red-600").
const UTILITY_PREFIXES = [
  'bg', 'text', 'border', 'ring', 'outline', 'fill', 'stroke', 'from', 'via', 'to',
  'shadow', 'divide', 'accent', 'caret', 'decoration', 'placeholder',
];
const UTILITY_REGEX = new RegExp(
  `(?<![\\w-])(${UTILITY_PREFIXES.join('|')})-(${[...BANNED_PALETTES, ...PENDING_PALETTES].join('|')})-(\\d+)`,
  'g'
);

// Strip comments so legitimate documentation doesn't trigger violations.
const COMMENT_PATTERNS = [/\/\/.*/g, /\/\*[\s\S]*?\*\//g, /<!--[\s\S]*?-->/g];

interface PaletteViolation {
  file: string;
  line: number;
  utility: string;
  palette: string;
}

interface ScanResult {
  violations: PaletteViolation[];
  /** True when at least one utility was let through by the gradient exemption. */
  usedGradientException: boolean;
}

/** Find banned palette utilities in one file's text. `file` is relative to src/app. */
function findPaletteViolations(content: string, file: string): ScanResult {
  const gradientExempt = GRADIENT_EXCEPTION_FILES.includes(file.split(path.sep).join('/'));
  const violations: PaletteViolation[] = [];
  let usedGradientException = false;

  const lines = content.split('\n');
  for (let lineNum = 0; lineNum < lines.length; lineNum++) {
    let line = lines[lineNum];
    for (const pattern of COMMENT_PATTERNS) {
      line = line.replace(pattern, '');
    }
    const isGradientLine = gradientExempt && GRADIENT_BACKGROUND.test(line);

    for (const match of line.matchAll(UTILITY_REGEX)) {
      const [utility, prefix, palette] = match;
      if (!BANNED_PALETTES.includes(palette)) continue; // pending palettes are allowed for now
      if (isGradientLine && GRADIENT_STOP_PREFIXES.has(prefix)) {
        usedGradientException = true;
        continue;
      }
      violations.push({ file, line: lineNum + 1, utility, palette });
    }
  }

  return { violations, usedGradientException };
}

describe('color token hygiene', () => {
  // Scans all of src/app, so it takes the scan timeout, not the 5 s default
  // (see source-tree.ts).
  it('src/app should not use raw Tailwind palettes (ratchet enforcement)', () => {
    // Files to skip:
    // - *.spec.ts (tests themselves)
    // - chart-colors.constants.ts (resolved colors for Chart.js, not Tailwind utilities)
    // - generated/* (machine-written, excluded from enforcement)
    const SKIP_FILES = [/\.spec\.ts$/i, /^chart-colors\.constants\.ts$/i];

    const appDir = fromProjectRoot('src/app');
    const files = readSourceTree(appDir, {
      extensions: ['.ts', '.html'],
      skipDirs: ['generated'],
      skipFile: (name) => SKIP_FILES.some((p) => p.test(name)),
    });

    const violations: PaletteViolation[] = [];
    const unusedExceptions = new Set(GRADIENT_EXCEPTION_FILES);

    for (const { relativePath, content } of files) {
      // Most files use no banned palette at all. Stripping comments and the
      // gradient exemption only ever remove matches, so a file whose raw
      // text has none has nothing to report. search() ignores lastIndex.
      if (content.search(UTILITY_REGEX) === -1) continue;
      const result = findPaletteViolations(content, relativePath);
      violations.push(...result.violations);
      if (result.usedGradientException) {
        unusedExceptions.delete(relativePath.split(path.sep).join('/'));
      }
    }

    const message = [
      'Raw Tailwind palette utilities found in src/app (not yet migrated to tokens):',
      '',
      ...violations.map((v) => `  ${v.file}:${v.line}\n    ${v.utility} (palette: ${v.palette})`),
      '',
      'Fix: Map each utility to the appropriate token per .kiro/steering/tailwind-colors.md section 6:',
      '  - Brand accent/interactive: primary-*, secondary-*, tertiary-*',
      '  - Status meaning: state-danger-*, state-warning-*, state-success-*, state-info-*',
      '  - Category identity: vendor-*, filetype-*, metric-*, category-accent-*, accent-1…10, star-*',
      '',
      'If it is a stop of a decorative two-color gradient, add the file to',
      'GRADIENT_EXCEPTION_FILES in this spec.',
    ].join('\n');
    expect(violations, message).toHaveLength(0);

    expect(
      [...unusedExceptions],
      'GRADIENT_EXCEPTION_FILES entries that no longer exempt anything — remove them:'
    ).toHaveLength(0);
  }, SOURCE_TREE_SCAN_TIMEOUT_MS);
});

describe('findPaletteViolations (guard self-test)', () => {
  const ordinaryFile = 'shared/components/example.component.ts';
  const exemptFile = GRADIENT_EXCEPTION_FILES[0];

  it('flags a banned palette and reports the palette name and full utility', () => {
    const { violations } = findPaletteViolations('<div class="p-4 bg-red-600"></div>', ordinaryFile);
    expect(violations).toEqual([
      { file: ordinaryFile, line: 1, utility: 'bg-red-600', palette: 'red' },
    ]);
  });

  it('flags utilities behind variants and the important modifier, on the right line', () => {
    const content = ['<p>', '  class="dark:hover:text-sky-700 !border-amber-200"', '</p>'].join('\n');
    const { violations } = findPaletteViolations(content, ordinaryFile);
    expect(violations.map((v) => [v.line, v.utility, v.palette])).toEqual([
      [2, 'text-sky-700', 'sky'],
      [2, 'border-amber-200', 'amber'],
    ]);
  });

  it('ignores neutrals, unlisted palettes, comments and non-utility words', () => {
    const content = [
      'class="bg-gray-100 text-white ring-blue-500"',
      '// bg-red-600 in a line comment',
      '/* text-rose-500 in a block comment */',
      '<!-- border-teal-400 in an HTML comment -->',
      'const label = "goto-red-1 into-sky-2";',
    ].join('\n');
    expect(findPaletteViolations(content, ordinaryFile).violations).toEqual([]);
  });

  it('exempts gradient stops only on a gradient line in an allowlisted file', () => {
    const gradient = 'class="bg-linear-to-br from-blue-700 to-sky-500 dark:to-sky-700"';

    const exempt = findPaletteViolations(gradient, exemptFile);
    expect(exempt.violations).toEqual([]);
    expect(exempt.usedGradientException).toBe(true);

    // Same gradient, file not on the list.
    expect(findPaletteViolations(gradient, ordinaryFile).violations.map((v) => v.utility)).toEqual([
      'to-sky-500',
      'to-sky-700',
    ]);

    // Allowlisted file, but not a gradient stop, or no gradient background.
    expect(
      findPaletteViolations('class="bg-linear-to-r text-sky-500"', exemptFile).violations.map((v) => v.utility)
    ).toEqual(['text-sky-500']);
    expect(
      findPaletteViolations('class="from-indigo-500 to-sky-500"', exemptFile).violations.map((v) => v.utility)
    ).toEqual(['from-indigo-500', 'to-sky-500']);
  });
});
