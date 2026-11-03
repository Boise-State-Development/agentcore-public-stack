// Prompt preview parity (handoffs spec §10): the review sheet's preview is built by the same rules
// as the server's first-turn message. The fixture is generated from the Python
// (`apis.shared.handoff.context`) and checked on that side too, so a change on either side fails
// one of the two tests until the fixture is regenerated and both agree.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fromProjectRoot } from '../../../../testing/project-root';
import { buildHandoffMessage, extractHandoffContext, wrapHandoffContext } from './handoff-prompt';

interface FixtureCase {
  name: string;
  card: string;
  title: string;
  request: string;
  contextBlock: string;
  prompt: string;
}

const fixture = JSON.parse(
  readFileSync(fromProjectRoot('src/app/session/services/handoff/handoff-prompt.fixture.json'), 'utf8'),
) as { cases: FixtureCase[] };

describe('handoff prompt parity with the server', () => {
  it('has cases to check', () => {
    expect(fixture.cases.length).toBeGreaterThan(5);
  });

  for (const c of fixture.cases) {
    it(`wraps and joins byte for byte: ${c.name}`, () => {
      const block = wrapHandoffContext(c.card, c.title);
      expect(block).toBe(c.contextBlock);
      expect(buildHandoffMessage(block, c.request)).toBe(c.prompt);
    });
  }
});

describe('extractHandoffContext', () => {
  it('returns the card a persisted first message carried', () => {
    const c = fixture.cases[0];
    expect(extractHandoffContext(c.prompt)).toBe(c.card.trim());
  });

  it('returns null for a message without the wrapper (a live bubble holds only the request)', () => {
    expect(extractHandoffContext('draft a rubric for this')).toBeNull();
    expect(extractHandoffContext('see <handoff_context from_conversation="x"> later')).toBeNull();
  });
});
