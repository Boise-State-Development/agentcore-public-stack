import { describe, it, expect } from 'vitest';
import {
  isChildSelected,
  isItemSelected,
  isWholeItemSelected,
  selectedChildCount,
  toggleChild,
  toggleItem,
  withoutItem,
} from './tool-selection';

/**
 * The invariant worth pinning: a fully-selected server is stored as the **bare**
 * ref, never as N scoped ones. Get it wrong and every existing record rewrites its
 * bindings the first time someone opens a form, for no change the author made.
 */

const CANVAS = ['list_courses', 'list_rubrics', 'grade_submission'];

function sorted(set: ReadonlySet<string>): string[] {
  return [...set].sort();
}

describe('tool-selection', () => {
  describe('a whole-server selection', () => {
    const whole = new Set(['canvas', 'calculator']);

    it('reads as selected, whole, with every tool on', () => {
      expect(isItemSelected(whole, 'canvas')).toBe(true);
      expect(isWholeItemSelected(whole, 'canvas')).toBe(true);
      expect(isChildSelected(whole, 'canvas', 'grade_submission')).toBe(true);
      expect(selectedChildCount(whole, 'canvas', CANVAS)).toBe(3);
    });

    it('narrows to scoped refs when one tool is turned off', () => {
      const next = toggleChild(whole, 'canvas', CANVAS, 'grade_submission');
      expect(sorted(next)).toEqual(['calculator', 'canvas::list_courses', 'canvas::list_rubrics']);
      expect(isWholeItemSelected(next, 'canvas')).toBe(false);
      expect(isItemSelected(next, 'canvas')).toBe(true);
    });

    it('collapses back to the bare ref when the last tool is turned on again', () => {
      const narrowed = toggleChild(whole, 'canvas', CANVAS, 'grade_submission');
      expect(sorted(toggleChild(narrowed, 'canvas', CANVAS, 'grade_submission'))).toEqual([
        'calculator',
        'canvas',
      ]);
    });

    it('deselects the server when its last tool is turned off', () => {
      let next: ReadonlySet<string> = whole;
      for (const name of CANVAS) next = toggleChild(next, 'canvas', CANVAS, name);
      expect(sorted(next)).toEqual(['calculator']);
      expect(isItemSelected(next, 'canvas')).toBe(false);
    });
  });

  describe('a scoped selection', () => {
    const scoped = new Set(['canvas::list_courses', 'canvas::list_rubrics']);

    it('reads as selected but not whole', () => {
      expect(isItemSelected(scoped, 'canvas')).toBe(true);
      expect(isWholeItemSelected(scoped, 'canvas')).toBe(false);
      expect(selectedChildCount(scoped, 'canvas', CANVAS)).toBe(2);
      expect(isChildSelected(scoped, 'canvas', 'grade_submission')).toBe(false);
    });

    it('drops every scoped ref when the row is toggled off', () => {
      expect(sorted(toggleItem(scoped, 'canvas'))).toEqual([]);
    });

    it('binds the whole server when the row is toggled back on', () => {
      expect(sorted(toggleItem(toggleItem(scoped, 'canvas'), 'canvas'))).toEqual(['canvas']);
    });
  });

  it('leaves other items alone', () => {
    const set = new Set(['canvas::list_courses', 'canvas_faculty', 'calculator']);
    // `canvas_faculty` shares a prefix with `canvas` but is a different item.
    expect(sorted(withoutItem(set, 'canvas'))).toEqual(['calculator', 'canvas_faculty']);
    expect(isItemSelected(new Set(['canvas_faculty']), 'canvas')).toBe(false);
  });

  it('never mutates its input', () => {
    const set = new Set(['calculator']);
    toggleItem(set, 'canvas');
    toggleChild(set, 'canvas', CANVAS, 'list_courses');
    withoutItem(set, 'calculator');
    expect(sorted(set)).toEqual(['calculator']);
  });
});
