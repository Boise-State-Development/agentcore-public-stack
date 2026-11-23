import { describe, expect, it } from 'vitest';
import {
  SkillVersionSummary,
  groupVersions,
  versionAuthorLabel,
  versionTriggerLabel,
} from './skill-version-history.model';

function row(version: number, overrides: Partial<SkillVersionSummary> = {}): SkillVersionSummary {
  return {
    version,
    createdAt: `2026-11-23T10:${String(version).padStart(2, '0')}:00+00:00`,
    trigger: 'file',
    displayName: 'Grant Review',
    fileCount: 1,
    isCurrent: false,
    createdByYou: true,
    ...overrides,
  };
}

describe('versionTriggerLabel', () => {
  it('names what cut a version, and which version a restore brought back', () => {
    expect(versionTriggerLabel(row(1, { trigger: 'save' }))).toBe('Saved');
    expect(versionTriggerLabel(row(2, { trigger: 'authoring' }))).toBe('Changed by an agent');
    expect(versionTriggerLabel(row(3, { trigger: 'restore', restoredFrom: 1 }))).toBe(
      'Restored from version 1',
    );
  });

  it('reads a row from before triggers were recorded as a pin', () => {
    expect(versionTriggerLabel(row(1, { trigger: null }))).toBe('Pinned by a project or agent');
  });
});

describe('versionAuthorLabel', () => {
  it('says You or Someone else on My Skills, and the email on the catalog', () => {
    expect(versionAuthorLabel(row(1))).toBe('You');
    expect(versionAuthorLabel(row(1, { createdByYou: false }))).toBe('Someone else');
    expect(
      versionAuthorLabel(row(1, { createdByYou: null, createdByEmail: 'admin@example.com' })),
    ).toBe('admin@example.com');
  });
});

describe('groupVersions', () => {
  it('collapses a run of file changes by one author within ten minutes', () => {
    const groups = groupVersions([row(5), row(4), row(3), row(2, { trigger: 'save' })]);
    expect(groups.map((g) => g.rows.map((r) => r.version))).toEqual([[5, 4, 3], [2]]);
    expect(groups[0].key).toBe(5);
  });

  it('breaks a run on another author, another trigger or a gap over ten minutes', () => {
    const rows = [
      row(6, { createdAt: '2026-11-23T12:00:00+00:00' }),
      row(5, { createdAt: '2026-11-23T11:00:00+00:00' }),
      row(4, { createdByYou: false }),
      row(3, { trigger: 'authoring' }),
    ];
    expect(groupVersions(rows).map((g) => g.rows.length)).toEqual([1, 1, 1, 1]);
  });

  it('never groups saves', () => {
    const rows = [row(2, { trigger: 'save' }), row(1, { trigger: 'save' })];
    expect(groupVersions(rows).map((g) => g.rows.length)).toEqual([1, 1]);
  });
});
