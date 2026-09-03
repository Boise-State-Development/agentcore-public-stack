// @vitest-environment node
//
// Surface literal hygiene guard (Task 8) — closes the hole
// color-tokens.spec.ts can't see: its utility-class regex requires a
// trailing numeric step (`bg-{palette}-{step}`), so `bg-white`, bare hex
// literals like `background: #f1f2f4`, and Tailwind arbitrary-value
// syntax like `bg-[#272822]` all slip through untouched, and its scan
// root is `src/app` (styles.css is outside it entirely).
//
// This spec scans component `styles:`/`style:` template-literal blocks and
// standalone `.css` files under `src/app` for literal `background` /
// `background-color` declarations using `white`, `black`, or a bare hex
// value, and fails if any are found outside the documented Prism
// exception. A failure here means a hardcoded surface color crept back
// in — reach for `var(--color-white)` / `var(--color-gray-*)` instead
// (the brand-configurable neutral ramp — see generate-surface-theme.ts),
// or `var(--color-{role}-*)` for a brand accent.
import { describe, expect, it } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

describe('surface literal hygiene', () => {
  it('src/app should not use literal white/black/hex values in background declarations (Prism okaidia exempted)', () => {
    // Documented exception: the Prism okaidia code-editor theme's fixed
    // background, intentionally decoupled from the app's light/dark mode
    // (see artifact-source.component.ts's header comment).
    const ALLOWLISTED_HEX = ['#272822'];

    const SKIP_PATTERNS = [/\.spec\.ts$/i, /generated[\\/]/i];

    // Matches inline CSS `background: white;` / `background-color: #f1f2f4;`
    // declarations, and Tailwind arbitrary-value bracket syntax
    // `bg-[#272822]`. Deliberately does NOT flag the plain `bg-white` /
    // `bg-black` utility classes — those are already-allowed neutrals per
    // color-tokens.spec.ts's ALLOWED_NEUTRALS (white/black are not part of
    // the themed surface, and now resolve through the brand-configurable
    // `--color-white` var anyway). This guard's job is narrower: catching
    // literal color *values* that bypass any CSS variable entirely.
    const declarationRegex = /background(?:-color)?\s*:\s*(white|black|#[0-9a-fA-F]{3,8})\b/gi;
    const arbitraryBgRegex = /\bbg-\[(#[0-9a-fA-F]{3,8})\]/g;

    const commentPatterns = [/\/\/.*/g, /\/\*[\s\S]*?\*\//g, /<!--[\s\S]*?-->/g];

    const appDir = path.join(__dirname);
    const files: string[] = [];

    function walkDir(dir: string): void {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (SKIP_PATTERNS.some((p) => p.test(fullPath))) continue;
        if (entry.isDirectory()) {
          walkDir(fullPath);
        } else if (/\.(ts|css|html)$/.test(entry.name)) {
          files.push(fullPath);
        }
      }
    }

    walkDir(appDir);

    const violations: Array<{ file: string; line: number; match: string }> = [];

    for (const file of files) {
      const content = fs.readFileSync(file, 'utf-8');
      const lines = content.split('\n');

      for (let lineNum = 0; lineNum < lines.length; lineNum++) {
        let line = lines[lineNum];
        for (const pattern of commentPatterns) {
          line = line.replace(pattern, '');
        }

        for (const regex of [declarationRegex, arbitraryBgRegex]) {
          regex.lastIndex = 0;
          let match: RegExpExecArray | null;
          while ((match = regex.exec(line)) !== null) {
            const hexOrKeyword = match[1]?.toLowerCase();
            if (hexOrKeyword && ALLOWLISTED_HEX.includes(`#${hexOrKeyword.replace(/^#/, '')}`)) {
              continue;
            }
            violations.push({ file, line: lineNum + 1, match: match[0] });
          }
        }
      }
    }

    if (violations.length > 0) {
      const message = [
        'Literal white/black/hex background values found in src/app (not using a Brand_Surface token):',
        '',
        ...violations.map((v) => `  ${path.relative(appDir, v.file)}:${v.line}\n    ${v.match}`),
        '',
        'Fix: use var(--color-white) or var(--color-gray-{step}) (the brand-configurable neutral ramp),',
        'or var(--color-{role}-*) for a brand accent. See src/branding/README.md "Using colors in components".',
        '',
        'If this is the Prism okaidia code-editor background exception, add it to ALLOWLISTED_HEX above',
        'with a comment explaining why it is exempt.',
      ].join('\n');

      expect(violations, message).toHaveLength(0);
    }
  });
});
