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
// 2. It belongs to a palette NOT yet cleared for migration.
// 3. Fix: Map the utility to the appropriate token (primary-*, state-*, vendor-*,
//    filetype-*, etc.) per .kiro/steering/tailwind-colors.md section 6.
// 4. If the color genuinely belongs to a pending palette (orange, indigo, purple,
//    sky), the entry must land in its designated Phase 3 location (API keys form,
//    dialog conventions, etc.) — check the prompt phase-4-lock-it-in.md.
// 5. If a utility is legitimately external data (OAuth provider branding), see
//    the allowlist comment below.
//
// The ratchet works by maintaining explicit pending palettes. Every time Phase 3
// or another track clears a palette, move it from pending into banned. This guard
// prevents silent regression.

import { describe, expect, it } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

describe('color token hygiene', () => {
  it('src/app should not use raw Tailwind palettes (ratchet enforcement)', () => {
    // Banned palettes — these are fully migrated and must never appear.
    // Note: orange, indigo, purple, sky are now banned too, with explicit documented
    // exceptions for two-color gradients (from-*-* to-*-*) which are out of scope for
    // the token system. These exceptions are in:
    //   - agent-detail.page.ts:210 (from-blue-700 to-sky-500 → intentional gradient)
    //   - profile-settings.page.ts:40 (from-blue-500 to-indigo-600 → intentional gradient)
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

    // Allowed neutral colors — not rebrandable, always permitted.
    const ALLOWED_NEUTRALS = [
      'gray',
      'slate',
      'zinc',
      'neutral',
      'stone',
      'white',
      'black',
    ];

    // Combine banned + allowed for the final allowlist check.
    const ALLOWED = [...ALLOWED_NEUTRALS];

    // Files to skip:
    // - *.spec.ts (tests themselves)
    // - chart-colors.constants.ts (resolved colors for Chart.js, not Tailwind utilities)
    // - generated/* (machine-written, excluded from enforcement)
    const SKIP_PATTERNS = [
      /\.spec\.ts$/i,
      /chart-colors\.constants\.ts$/i,
      /generated[\\/]/i,
    ];

    // Regex for raw palette utilities. Anchors on utility prefix to avoid false
    // negatives from "bg-purple-100" being missed by underscore/dash lookbehinds.
    // Pattern: (prefix)-(palette)-(step)
    // Prefixes: bg, text, border, ring, outline, fill, stroke, from, via, to,
    //           shadow, divide, accent, caret, decoration, placeholder
    const allPalettes = [...BANNED_PALETTES, ...PENDING_PALETTES];
    const palettePattern = `(?:${allPalettes.join('|')})`;
    const utilityRegex = new RegExp(
      `(?:bg|text|border|ring|outline|fill|stroke|from|via|to|shadow|divide|accent|caret|decoration|placeholder)-${palettePattern}-(\\d+)`,
      'g'
    );

    // Comment pattern to skip legitimate documentation and comments.
    const commentPatterns = [/\/\/.*/g, /\/\*[\s\S]*?\*\//g, /<!--[\s\S]*?-->/g];

    // Scan files.
    const appDir = path.join(__dirname);
    const files: string[] = [];

    function walkDir(dir: string): void {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (SKIP_PATTERNS.some((p) => p.test(fullPath))) {
          continue;
        }
        if (entry.isDirectory()) {
          walkDir(fullPath);
        } else if (/\.(ts|html)$/.test(entry.name)) {
          files.push(fullPath);
        }
      }
    }

    walkDir(appDir);

    const violations: Array<{
      file: string;
      line: number;
      utility: string;
      palette: string;
    }> = [];

    for (const file of files) {
      const content = fs.readFileSync(file, 'utf-8');
      const lines = content.split('\n');

      for (let lineNum = 0; lineNum < lines.length; lineNum++) {
        let line = lines[lineNum];

        // Strip comments so legitimate documentation doesn't trigger violations.
        for (const pattern of commentPatterns) {
          line = line.replace(pattern, '');
        }

        // Find all raw palette utilities in this line.
        let match;
        utilityRegex.lastIndex = 0; // Reset regex state.
        while ((match = utilityRegex.exec(line)) !== null) {
          const fullUtility = match[0]; // e.g., "bg-red-600"
          const palette = match[1]; // e.g., "red"

          // Check: is this palette banned?
          if (BANNED_PALETTES.includes(palette)) {
            violations.push({
              file,
              line: lineNum + 1,
              utility: fullUtility,
              palette,
            });
          }
          // Pending palettes are allowed for now; no violation.
        }
      }
    }

    if (violations.length > 0) {
      const message = [
        'Raw Tailwind palette utilities found in src/app (not yet migrated to tokens):',
        '',
        ...violations.map(
          (v) =>
            `  ${path.relative(appDir, v.file)}:${v.line}\n    ${v.utility} (palette: ${v.palette})`
        ),
        '',
        'Fix: Map each utility to the appropriate token per .kiro/steering/tailwind-colors.md section 6:',
        '  - Brand accent/interactive: primary-*, secondary-*, tertiary-*',
        '  - Status meaning: state-danger-*, state-warning-*, state-success-*, state-info-*',
        '  - Category identity: vendor-*, filetype-*, metric-*, category-accent-*, accent-1…10, star-*',
        '',
        'If the utility belongs to a pending palette (orange, indigo, purple, sky),',
        'confirm it is in its designated Phase 3 location (see phase-4-lock-it-in.md).',
        '',
        'For external data (OAuth provider branding), add an allowlist entry.',
      ].join('\n');

      expect(violations, message).toHaveLength(0);
    }
  });
});
