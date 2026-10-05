import { describe, expect, it } from 'vitest';
import { personLabel } from './person';

describe('personLabel', () => {
  it('prefers the name', () => {
    expect(personLabel('Ada Lovelace', 'ada@example.edu')).toBe('Ada Lovelace');
  });

  it('falls back to the email when there is no name', () => {
    expect(personLabel(null, 'ada@example.edu')).toBe('ada@example.edu');
    expect(personLabel(undefined, 'ada@example.edu')).toBe('ada@example.edu');
    expect(personLabel('  ', 'ada@example.edu')).toBe('ada@example.edu');
  });

  it('is empty when there is neither', () => {
    expect(personLabel(null, null)).toBe('');
  });
});
