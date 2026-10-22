import type { MemoryEntry, Project } from '../app/projects/models/project.model';

/** Shared fixtures for the Memory tab specs (shared-projects 2.8). */

export const MEMORY_PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: 'Olive Owner',
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

export const MEMORY_LIMITS = {
  fileHardCapTokens: 8000,
  fileSoftThresholdTokens: 6000,
  projectIndexBudgetTokens: 2000,
  personalIndexBudgetTokens: 1000,
};

export function memoryEntry(slug: string, extra: Partial<MemoryEntry> = {}): MemoryEntry {
  return {
    slug,
    description: '',
    updated: '2026-10-01T00:00:00Z',
    updatedBy: '',
    updatedByName: null,
    aliases: [],
    tokens: 100,
    tokensMethod: 'count',
    itemCount: 1,
    archived: false,
    version: 1,
    ...extra,
  };
}
