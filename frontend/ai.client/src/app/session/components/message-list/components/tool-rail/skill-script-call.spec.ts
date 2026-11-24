import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fromProjectRoot } from '../../../../../../testing/project-root';
import type { ToolCallDisplay } from './tool-rail.model';
import { describeToolCall } from './tool-summary';
import { scriptRefused, scriptVersion, scriptVersionFromText, skillScriptCall } from './skill-script-call';
import { trustLine } from './skill-script-trust.component';

const fixture = JSON.parse(
  readFileSync(
    fromProjectRoot('src/app/session/components/message-list/components/tool-rail/script-version.fixture.json'),
    'utf8',
  ),
) as { cases: { name: string; resultText: string; version: string | null }[] };

function call(over: Partial<ToolCallDisplay> & { text?: string } = {}): ToolCallDisplay {
  const { text, ...rest } = over;
  return {
    id: 't1',
    toolName: 'run_code',
    input: { skill_name: 'pdf', path: 'scripts/fill_fillable_fields.py' },
    status: 'complete',
    result: text === undefined ? undefined : { status: 'success', content: [{ text }] },
    ...rest,
  };
}

describe('the Script version line, against the Python (script-version.fixture.json)', () => {
  for (const c of fixture.cases) {
    it(c.name, () => {
      expect(scriptVersionFromText(c.resultText)).toBe(c.version);
    });
  }
});

describe('skill script calls in the rail (skill-code-sandbox §8.6)', () => {
  it('is a call that names a skill and a path, on run_code or the Word tools', () => {
    expect(skillScriptCall(call())).toEqual({ skill: 'pdf', path: 'scripts/fill_fillable_fields.py', filename: 'fill_fillable_fields.py' });
    expect(skillScriptCall(call({ toolName: 'create_word_document' }))?.skill).toBe('pdf');
    expect(skillScriptCall(call({ input: { code: 'print(1)' } }))).toBeNull();
    expect(skillScriptCall(call({ toolName: 'read_skill_file' }))).toBeNull();
  });

  it('says what ran, from which skill', () => {
    expect(describeToolCall(call({ status: 'pending' }))).toBe('Running fill_fillable_fields.py from pdf');
    expect(describeToolCall(call({ text: 'Ran scripts/fill_fillable_fields.py: finished (exit code 0).\nScript version: 3f2a9c1e' }))).toBe(
      'Ran fill_fillable_fields.py from pdf',
    );
    const refused = call({
      text: "'pdf/scripts/fill_fillable_fields.py' didn't run: its scripts haven't been allowed in this agent. Nothing ran.",
    });
    expect(scriptRefused(refused)).toBe(true);
    expect(describeToolCall(refused)).toBe("Didn't run fill_fillable_fields.py from pdf");
    expect(describeToolCall(call({ status: 'error' }))).toBe("Didn't run fill_fillable_fields.py from pdf");
  });

  it('reads the version only from its own line', () => {
    expect(scriptVersion(call({ text: 'Output:\nScript version: 3f2a9c1e' }))).toBe('3f2a9c1e');
    expect(scriptVersion(call({ text: 'print("Script version: 3f2a9c1e is fine")' }))).toBeNull();
  });

  it('writes the expanded trust line for each path', () => {
    const base = { displayName: 'PDF', author: 'Pat', version: '3f2a9c1e', reviewer: null, reviewedAt: null, method: null, provenance: null };
    expect(trustLine({ ...base, trustedBy: 'own' })).toBe('You wrote this skill');
    expect(trustLine({ ...base, trustedBy: 'imported', provenance: { type: 'github', owner: 'anthropics', repo: 'skills' } })).toBe(
      'You imported this skill from anthropics/skills',
    );
    expect(trustLine({ ...base, trustedBy: 'imported', provenance: { type: 'zip' } })).toBe('You imported this skill from a zip file');
    expect(trustLine({ ...base, trustedBy: 'admin' })).toBe('From the admin catalog');
    expect(trustLine({ ...base, trustedBy: 'reviewed', method: 'review', reviewer: 'Sam', reviewedAt: '2026-11-23T10:00:00Z' })).toBe(
      'Reviewed by Sam, Nov 23, 2026',
    );
    expect(trustLine({ ...base, trustedBy: 'reviewed', method: 'authored', reviewedAt: '2026-11-23T10:00:00Z' })).toBe(
      'Shared by Pat, Nov 23, 2026',
    );
    expect(trustLine({ ...base, trustedBy: null })).toBeNull();
  });
});
