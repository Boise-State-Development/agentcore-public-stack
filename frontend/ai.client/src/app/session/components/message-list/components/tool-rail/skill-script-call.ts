import type { ToolCallDisplay } from './tool-rail.model';

/**
 * A tool call that ran (or tried to run) a skill's own script by name, rather than code
 * the model wrote (docs/specs/skill-code-sandbox.md §8.6): `run_code` and the Word
 * tools, called with `skill_name` + `path`. The rail names the script and the skill, and
 * says whose decision let it run, so a member can see what ran on their behalf.
 */

/** The tools whose `skill_name` + `path` run a bound skill's script. */
const SCRIPT_TOOLS = new Set(['run_code', 'create_word_document', 'modify_word_document']);

/** The result line the tool writes (`run_code_tool.py`); pinned by `script-version.fixture.json`. */
const VERSION_LINE = /^Script version: ([0-9a-f]{8})\s*$/m;

/** What every refusal says (`skill_scripts.trust_refusal`, spec §8.7). */
const REFUSAL = /' didn't run: /;

export interface SkillScriptCall {
  /** `skill_name` as the model passed it: the skill's activation name. */
  skill: string;
  /** `path` as passed, e.g. `scripts/fill_fillable_fields.py`. */
  path: string;
  /** The last part of the path, for the collapsed line. */
  filename: string;
}

/** The script a call ran by name, or null for any other call (model-written code included). */
export function skillScriptCall(call: ToolCallDisplay): SkillScriptCall | null {
  if (!SCRIPT_TOOLS.has(call.toolName)) return null;
  const skill = call.input?.['skill_name'];
  const path = call.input?.['path'];
  if (typeof skill !== 'string' || !skill.trim() || typeof path !== 'string' || !path.trim()) return null;
  const filename = path.split('/').filter(Boolean).pop() ?? path;
  return { skill: skill.trim(), path: path.trim(), filename };
}

function resultText(call: ToolCallDisplay): string {
  return (call.result?.content ?? [])
    .map((c) => c.text ?? '')
    .filter(Boolean)
    .join('\n');
}

/** "3f2a9c1e" from the result's `Script version:` line, or null when it has none. */
export function scriptVersionFromText(text: string): string | null {
  return VERSION_LINE.exec(text)?.[1] ?? null;
}

export function scriptVersion(call: ToolCallDisplay): string | null {
  return scriptVersionFromText(resultText(call));
}

/** The call was refused before anything ran (a trust refusal, §8.7). */
export function scriptRefused(call: ToolCallDisplay): boolean {
  return REFUSAL.test(resultText(call));
}

/** What the tool writes first for a script it started: "Ran …: finished|failed (exit code n)." */
const RAN = /^Ran .+: (?:finished|failed) \(exit code -?\d+\)\./m;

/**
 * The script started, whatever its exit code. A script that exits non-zero comes back as
 * a tool error, but it still ran in the sandbox; only a refusal or a call that never
 * started is "Didn't run".
 */
export function scriptRan(call: ToolCallDisplay): boolean {
  const text = resultText(call);
  return !REFUSAL.test(text) && (RAN.test(text) || VERSION_LINE.test(text));
}

/**
 * "Running fill.py from pdf", "Ran fill.py from pdf", "Ran fill.py from pdf, which failed"
 * (it started and exited non-zero), "Didn't run fill.py from pdf" (refused, or a tool error
 * before it started).
 */
export function describeSkillScriptCall(
  script: SkillScriptCall,
  state: 'running' | 'ok' | 'failed' | 'refused',
): string {
  const what = `${script.filename} from ${script.skill}`;
  if (state === 'running') return `Running ${what}`;
  if (state === 'ok') return `Ran ${what}`;
  if (state === 'failed') return `Ran ${what}, which failed`;
  return `Didn't run ${what}`;
}
