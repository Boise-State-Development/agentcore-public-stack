import { describe, expect, it } from 'vitest';

import { MentionableAgent } from '../../../agents/services/agent-mention.service';
import { matchSuggestedAgent } from './suggested-agent';

const agent = (agentId: string, name: string): MentionableAgent => ({ agentId, name, group: 'own' });

describe('matchSuggestedAgent', () => {
  const agents = [agent('a', 'Rubric Builder'), agent('b', 'Docx Dogfood'), agent('c', 'rubric builder II')];

  it('matches an exact name', () => {
    expect(matchSuggestedAgent('Rubric Builder', agents)?.agentId).toBe('a');
  });

  it('then the same name ignoring case and surrounding space', () => {
    expect(matchSuggestedAgent('  docx dogfood ', agents)?.agentId).toBe('b');
  });

  it('never guesses from a partial name', () => {
    expect(matchSuggestedAgent('Rubric', agents)).toBeUndefined();
    expect(matchSuggestedAgent('Builder', agents)).toBeUndefined();
  });

  it('asks when two Agents differ only in case', () => {
    expect(matchSuggestedAgent('RUBRIC BUILDER', [agent('a', 'Rubric Builder'), agent('z', 'rubric builder')])).toBeUndefined();
  });

  it('prefers the exact name over a case-folded twin', () => {
    expect(matchSuggestedAgent('rubric builder', [agent('a', 'Rubric Builder'), agent('z', 'rubric builder')])?.agentId).toBe(
      'z',
    );
  });

  it('answers nothing for no hint or no Agents', () => {
    expect(matchSuggestedAgent(undefined, agents)).toBeUndefined();
    expect(matchSuggestedAgent('   ', agents)).toBeUndefined();
    expect(matchSuggestedAgent('Rubric Builder', [])).toBeUndefined();
  });
});
