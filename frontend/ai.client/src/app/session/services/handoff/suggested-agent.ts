import { MentionableAgent } from '../../../agents/services/agent-mention.service';

/**
 * The Agent a suggestion's `agentHint` names (handoffs spec §4.4): an exact name first, then the
 * same name ignoring case and surrounding space. Only the Agents the user can `@`-mention count,
 * the same set the spec names ("your own and pinned Agents"). `undefined` when the model named
 * none or nothing matches, and the chip asks the user to pick.
 *
 * Deliberately no fuzzy match: a chip that confidently says "Hand off to Rubric Builder II" when
 * the model meant "Rubric Builder" sends work to the wrong Agent, and a picker costs one click.
 */
export function matchSuggestedAgent(
  hint: string | undefined,
  agents: readonly MentionableAgent[],
): MentionableAgent | undefined {
  const wanted = hint?.trim();
  if (!wanted) return undefined;
  const exact = agents.find((agent) => agent.name === wanted);
  if (exact) return exact;
  const folded = wanted.toLowerCase();
  const matches = agents.filter((agent) => agent.name.trim().toLowerCase() === folded);
  // Two Agents differing only in case are a coin toss: ask instead.
  return matches.length === 1 ? matches[0] : undefined;
}
