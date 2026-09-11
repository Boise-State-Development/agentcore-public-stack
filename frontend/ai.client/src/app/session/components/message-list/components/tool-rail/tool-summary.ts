import type { ToolCallDisplay } from './tool-rail.model';

/**
 * Deterministic, zero-cost descriptions of what a tool call did.
 *
 * This is the line the rail shows the instant a tool starts, and the line it
 * keeps forever if the model-generated summary never arrives (flag off, Nova
 * call failed, older conversation). It is computed entirely from data the
 * client already holds — the tool name, its input, its result — so it costs
 * nothing, needs no persistence, and survives a reload for free.
 *
 * WHY PATTERN-BASED, NOT A PER-TOOL TABLE
 * ---------------------------------------
 * Almost every tool in this platform arrives at runtime from an MCP server
 * (gateway targets, external servers, per-agent bindings). A registry keyed by
 * exact tool name would cover the handful of built-ins and leave `list_courses`
 * and `get_assignment_details` — the ones users actually see — rendering as
 * raw identifiers. So the default path reads the *shape* of the name, which is
 * near-universally `verb_subject` across the MCP ecosystem, and enriches it
 * with the cardinality of the result.
 *
 * If a specific tool ever deserves better than the pattern can infer, the
 * extension point is `describeToolCall` — branch on `call.toolName` before
 * falling through to the pattern. Deliberately not a registry service yet:
 * there is no tool today the pattern gets wrong, and an empty registry is a
 * seam to maintain rather than a feature.
 */

/** Leading verb tokens we recognize, mapped to their past-tense display form. */
const VERB_FORMS: ReadonlyMap<string, { ok: string; failed: string }> = new Map([
  ['list', { ok: 'Listed', failed: "Couldn't list" }],
  ['get', { ok: 'Read', failed: "Couldn't read" }],
  ['fetch', { ok: 'Fetched', failed: "Couldn't fetch" }],
  // Browser-shaped tools: "Ran browse web" is technically correct and reads
  // like a machine. These are common enough to be worth naming properly.
  ['browse', { ok: 'Browsed', failed: "Couldn't reach" }],
  ['open', { ok: 'Opened', failed: "Couldn't open" }],
  ['visit', { ok: 'Visited', failed: "Couldn't reach" }],
  ['navigate', { ok: 'Opened', failed: "Couldn't open" }],
  ['read', { ok: 'Read', failed: "Couldn't read" }],
  ['search', { ok: 'Searched', failed: "Couldn't search" }],
  ['query', { ok: 'Queried', failed: "Couldn't query" }],
  ['find', { ok: 'Found', failed: "Couldn't find" }],
  ['create', { ok: 'Created', failed: "Couldn't create" }],
  ['add', { ok: 'Added', failed: "Couldn't add" }],
  ['update', { ok: 'Updated', failed: "Couldn't update" }],
  ['modify', { ok: 'Updated', failed: "Couldn't update" }],
  ['edit', { ok: 'Edited', failed: "Couldn't edit" }],
  ['set', { ok: 'Set', failed: "Couldn't set" }],
  ['delete', { ok: 'Deleted', failed: "Couldn't delete" }],
  ['remove', { ok: 'Removed', failed: "Couldn't remove" }],
  ['send', { ok: 'Sent', failed: "Couldn't send" }],
  ['run', { ok: 'Ran', failed: "Couldn't run" }],
  ['execute', { ok: 'Ran', failed: "Couldn't run" }],
  ['generate', { ok: 'Generated', failed: "Couldn't generate" }],
  ['calculate', { ok: 'Calculated', failed: "Couldn't calculate" }],
  ['upload', { ok: 'Uploaded', failed: "Couldn't upload" }],
  ['download', { ok: 'Downloaded', failed: "Couldn't download" }],
]);

/**
 * Subject words that read badly pluralized or prefixed. "details" is already
 * plural; "knowledge_base" wants a "the".
 */
const ARTICLE_SUBJECTS = new Set([
  'knowledge base',
  'web',
  'workspace',
  'session',
  'calendar',
  'inbox',
]);

/**
 * Split an MCP tool name into a verb and a human subject.
 *
 * Handles `snake_case`, `camelCase`, `kebab-case` and the `server::tool`
 * scoped ids used for per-tool MCP enablement — the scope prefix is a routing
 * detail and never belongs in a sentence shown to a user.
 */
export function splitToolName(toolName: string): { verb: string; subject: string } {
  const bare = (toolName ?? '').split('::').pop() ?? '';
  const words = bare
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-.]+/g, ' ')
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);

  if (!words.length) return { verb: '', subject: '' };

  const [head, ...rest] = words;
  if (VERB_FORMS.has(head) && rest.length) {
    return { verb: head, subject: rest.join(' ') };
  }
  return { verb: '', subject: words.join(' ') };
}

/**
 * How many things the result appears to contain, or `undefined` when that
 * can't be told honestly.
 *
 * Only counts what is unambiguous: a top-level JSON array, or a single
 * array-valued property on a top-level object (the near-universal
 * `{items: [...]}` / `{courses: [...]}` envelope). Anything else returns
 * `undefined` rather than a guess — "Listed 1 assignments" from a
 * misidentified object is worse than "Listed assignments".
 */
export function resultCardinality(call: ToolCallDisplay): number | undefined {
  const content = call.result?.content;
  if (!content?.length) return undefined;

  for (const item of content) {
    const value = item.json ?? tryParse(item.text);
    if (Array.isArray(value)) return value.length;
    if (value && typeof value === 'object') {
      const arrays = Object.values(value as Record<string, unknown>).filter(
        Array.isArray,
      ) as unknown[][];
      if (arrays.length === 1) return arrays[0].length;
    }
  }
  return undefined;
}

function tryParse(text?: string): unknown {
  if (!text) return undefined;
  const trimmed = text.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return undefined;
  }
}

/**
 * Naive singularization, good enough for "4 assignments" -> "1 assignment".
 *
 * The `-es` strip is deliberately narrow (only the sibilant stems that really
 * take it): a blanket rule for nouns ending in "-ses" turns "courses" into
 * "cours", which is the kind of small wrongness that makes a whole feature
 * look unfinished.
 */
const ES_PLURAL = /(sses|shes|ches|xes|zes)$/;

function singularize(subject: string): string {
  if (subject.endsWith('ies')) return `${subject.slice(0, -3)}y`;
  if (ES_PLURAL.test(subject)) return subject.slice(0, -2);
  if (subject.endsWith('s') && !subject.endsWith('ss')) return subject.slice(0, -1);
  return subject;
}

function withArticle(subject: string): string {
  return ARTICLE_SUBJECTS.has(subject) ? `the ${subject}` : subject;
}

/**
 * One line describing a single tool call.
 *
 * A call still in flight is described in the present progressive ("Listing
 * assignments"), because the rail shows this while the tool runs and a
 * past-tense line on an unfinished call claims something that has not
 * happened yet.
 */
export function describeToolCall(call: ToolCallDisplay): string {
  const { verb, subject } = splitToolName(call.toolName);
  const failed = call.status === 'error' || call.result?.status === 'error';
  const running = call.status === 'pending';

  if (!verb) {
    // No recognizable verb: fall back to naming the tool's own subject.
    const label = subject || call.toolName || 'tool';
    if (failed) return `Couldn't run ${label}`;
    return running ? `Running ${label}` : `Ran ${label}`;
  }

  const forms = VERB_FORMS.get(verb)!;
  if (failed) return `${forms.failed} ${withArticle(subject)}`;

  if (running) {
    // "list" -> "Listing". Capitalized here rather than in
    // `presentProgressive` so that helper stays a pure grammar function.
    return `${capitalize(presentProgressive(verb))} ${withArticle(subject)}`;
  }

  const count = resultCardinality(call);
  if (typeof count === 'number' && (verb === 'list' || verb === 'search' || verb === 'query' || verb === 'find')) {
    const noun = count === 1 ? singularize(subject) : subject;
    return `${forms.ok} ${count} ${noun}`;
  }
  return `${forms.ok} ${withArticle(subject)}`;
}

/** Verbs whose final consonant doubles: get -> getting, run -> running. */
const DOUBLING_VERBS = new Set(['get', 'set', 'run']);

function capitalize(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word;
}

function presentProgressive(verb: string): string {
  if (verb.endsWith('e') && !verb.endsWith('ee')) return `${verb.slice(0, -1)}ing`;
  if (DOUBLING_VERBS.has(verb)) return `${verb}${verb.slice(-1)}ing`;
  return `${verb}ing`;
}

/**
 * One line describing a whole group of tool calls — the rail's collapsed
 * header when no model-generated summary is available.
 *
 * Groups are usually homogeneous ("three searches") or a short pipeline
 * ("list, then get details"), so the two cases are handled separately rather
 * than by chaining every call: a header that grows with the group defeats the
 * point of collapsing it.
 */
export function describeToolGroup(calls: readonly ToolCallDisplay[]): string {
  if (!calls.length) return '';
  if (calls.length === 1) return describeToolCall(calls[0]);

  const running = calls.find(c => c.status === 'pending');
  if (running) {
    const done = calls.filter(c => c.status !== 'pending').length;
    const label = describeToolCall(running);
    return done > 0 ? `${label} (${done} of ${calls.length} done)` : label;
  }

  const failures = calls.filter(
    c => c.status === 'error' || c.result?.status === 'error',
  ).length;

  const subjects = new Set(calls.map(c => splitToolName(c.toolName).subject));
  if (subjects.size === 1) {
    // Same subject throughout: report the work, not each call.
    const total = calls.reduce(
      (sum, c) => sum + (resultCardinality(c) ?? 0),
      0,
    );
    const subject = [...subjects][0];
    const line = total
      ? `Ran ${calls.length} lookups on ${withArticle(subject)} (${total} results)`
      : `Ran ${calls.length} lookups on ${withArticle(subject)}`;
    return failures ? `${line}, ${failures} failed` : line;
  }

  const head = describeToolCall(calls[0]);
  const rest = calls.length - 1;
  const line = `${head}, then ${rest} more step${rest === 1 ? '' : 's'}`;
  return failures ? `${line} (${failures} failed)` : line;
}
