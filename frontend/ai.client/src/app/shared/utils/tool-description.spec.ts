import { describe, it, expect } from 'vitest';
import { splitToolDescription } from './tool-description';

describe('splitToolDescription', () => {
  it('returns empty halves for nothing', () => {
    expect(splitToolDescription('')).toEqual({ summary: '', detail: '' });
    expect(splitToolDescription(null)).toEqual({ summary: '', detail: '' });
    expect(splitToolDescription(undefined)).toEqual({ summary: '', detail: '' });
  });

  it('treats a description with no sections as all summary', () => {
    const text = 'Renders a hand-drawn diagram using Excalidraw elements.';
    expect(splitToolDescription(text)).toEqual({ summary: text, detail: '' });
  });

  it('splits a real Gmail docstring at Args:', () => {
    // Verbatim from the prod catalog.
    const text = [
      "Send a new email from the authenticated user's mailbox.",
      '',
      '        For replies, pass `thread_id` to keep the message threaded.',
      '',
      '        Args:',
      '            to: Recipient email addresses.',
      '            subject: Subject line.',
    ].join('\n');

    const { summary, detail } = splitToolDescription(text);
    expect(summary).toBe(
      "Send a new email from the authenticated user's mailbox. For replies, pass `thread_id` to keep the message threaded.",
    );
    expect(detail.startsWith('Args:')).toBe(true);
    expect(detail).toContain('subject: Subject line.');
  });

  it('splits at Returns: when there is no Args:', () => {
    const text = "Fetch the authenticated student's advisors.\n\n        Returns:\n            Dictionary with advisors.";
    const { summary, detail } = splitToolDescription(text);
    expect(summary).toBe("Fetch the authenticated student's advisors.");
    expect(detail).toContain('Returns:');
  });

  it('unwraps the summary but preserves the detail’s lines', () => {
    const text = 'One\n        two\n        three.\n\n        Args:\n            a: first\n            b: second';
    const { summary, detail } = splitToolDescription(text);
    expect(summary).toBe('One two three.');
    expect(detail.split('\n').length).toBeGreaterThan(1);
  });

  it('ignores a section word used mid-sentence', () => {
    // "Note:" only counts at the start of a line — otherwise a normal sentence
    // would truncate the summary.
    const text = 'Use this when the note: field matters to the caller.';
    expect(splitToolDescription(text)).toEqual({ summary: text, detail: '' });
  });

  it('keeps a description that opens with a heading whole', () => {
    // No prose summary to extract — showing nothing would be worse.
    const text = 'Args:\n    a: first';
    const { summary, detail } = splitToolDescription(text);
    expect(summary).toBe(text);
    expect(detail).toBe('');
  });

  it('handles the NOTE: variant our servers actually use', () => {
    const text =
      "Fetch the student's academic programs.\n\n        NOTE: academic_career defaults to \"UGRD\".";
    const { summary, detail } = splitToolDescription(text);
    expect(summary).toBe("Fetch the student's academic programs.");
    expect(detail).toContain('UGRD');
  });

  it('cuts the worst real offender down to a readable line', () => {
    // class_search.search_classes is 3,019 characters in prod, of which 1,819
    // sit below the first section heading.
    const body = 'x'.repeat(1800);
    const text = `Search for classes by subject and term.\n\n        Args:\n${body}`;
    const { summary, detail } = splitToolDescription(text);
    expect(summary).toBe('Search for classes by subject and term.');
    expect(detail.length).toBeGreaterThan(1000);
  });
});
