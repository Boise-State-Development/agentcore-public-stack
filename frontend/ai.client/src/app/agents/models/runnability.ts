import { AgentRunnability } from './agent.model';

/**
 * D6's answer, as a sentence, in the one place both surfaces read it from.
 *
 * The detail page and the chat launch card ask the same question and must not drift into
 * two different phrasings of it — a user who sees "Not available to you" on one screen and
 * "You can't run this" on the next has been told there are two different problems.
 *
 * Every message names *what* is missing rather than saying "something": a user who cannot
 * act on the sentence has been told nothing useful.
 */
export function runnabilityMessage(runnability: AgentRunnability): string {
  if (runnability.state === 'ready') return 'Ready to run for you.';

  const names = runnability.missing.map((m) => `“${m.label}”`).join(', ');
  const verb = runnability.missing.length === 1 ? "isn't" : "aren't";
  const lead = 'Not available to you';
  return names ? `${lead} — ${names} ${verb} granted to your role.` : `${lead}.`;
}

/** The heroicon that goes with the sentence above. */
export function runnabilityIcon(runnability: AgentRunnability): string {
  return runnability.state === 'ready' ? 'heroCheckCircle' : 'heroNoSymbol';
}
