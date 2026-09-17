// @vitest-environment node
//
// Guards the one Angular mistake in this codebase that `tsc --noEmit`
// cannot see.
//
// A backtick inside a component's inline `template:` or `styles:` block
// closes the template literal early. TypeScript still typechecks the
// file — the truncated literal is a valid string, and the prose after it
// parses as *something* — so `npx tsc --noEmit` reports success and only
// the Angular compiler fails, with errors that point at the wrong thing
// entirely ("Cannot find name 'text'", "Expected 1 arguments, but got 3",
// "Incorrect number of arguments to @Component decorator").
//
// It is an easy mistake because the natural way to name a CSS class or a
// method in a comment is to quote it in backticks, exactly as one would
// anywhere else in the file. It has cost debugging time three separate
// times. This spec makes it a test failure with a message that names the
// real cause.
//
// The fix is always the same: write the identifier without backticks.
import { describe, expect, it } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import { fromProjectRoot } from '../testing/project-root';

/**
 * Walk `dir` for component sources, skipping generated output and specs.
 *
 * Specs are excluded for the same reason `surface-literal-guard.spec.ts`
 * excludes them: they are not compiled by the Angular compiler, and a
 * spec that quotes the pattern it looks for — this one does — would
 * otherwise report itself.
 */
function collect(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === 'generated') continue;
      collect(full, out);
    } else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.spec.ts')) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Index just past the backtick that closes the literal starting at
 * `start`, honouring backslash escapes.
 */
function endOfLiteral(source: string, start: number): number {
  for (let i = start; i < source.length; i++) {
    if (source[i] === '\\') {
      i++;
      continue;
    }
    if (source[i] === '`') return i;
  }
  return -1;
}

describe('inline template hygiene', () => {
  it('no component template: or styles: block contains a backtick', () => {
    const offenders: string[] = [];

    for (const file of collect(fromProjectRoot('src/app'))) {
      const source = fs.readFileSync(file, 'utf8');

      for (const key of ['template: `', 'styles: `']) {
        let cursor = 0;
        while (true) {
          const at = source.indexOf(key, cursor);
          if (at === -1) break;

          const literalStart = at + key.length;
          const close = endOfLiteral(source, literalStart);
          cursor = close === -1 ? source.length : close + 1;
          if (close === -1) continue;

          // A literal that really is the whole block is followed by the
          // decorator's next property or its closing brace. Anything
          // else means the backtick we found was a stray one inside the
          // block, and the "literal" stopped short.
          const after = source.slice(close + 1).trimStart();
          if (after.startsWith(',') || after.startsWith('}') || after.startsWith(';')) {
            continue;
          }

          const line = source.slice(0, close).split('\n').length;
          offenders.push(
            `${path.relative(fromProjectRoot('.'), file)}:${line} — ` +
              `stray backtick inside a ${key.replace(': `', '')} block ` +
              `(near: ${source.slice(Math.max(0, close - 40), close + 20).replace(/\n/g, ' ')})`,
          );
        }
      }
    }

    expect(
      offenders,
      'A backtick inside an inline template:/styles: block ends the ' +
        'template literal early. tsc passes; only `ng build` fails, and ' +
        'its errors name the wrong cause. Drop the backticks:\n' +
        offenders.join('\n'),
    ).toEqual([]);
  });
});
