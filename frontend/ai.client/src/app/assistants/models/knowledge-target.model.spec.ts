import { describe, it, expect } from 'vitest';
import { knowledgePaths } from './knowledge-target.model';

describe('knowledgePaths', () => {
  it('keeps an agent on its own routes', () => {
    expect(knowledgePaths('ast-1')).toEqual({
      documents: '/assistants/ast-1/documents',
      webSources: '/assistants/ast-1/web-sources',
      syncPolicies: '/assistants/ast-1/sync-policies',
    });
  });

  it('sends a project through its knowledge routes', () => {
    expect(knowledgePaths({ projectId: 'prj_1' })).toEqual({
      documents: '/projects/prj_1/knowledge',
      webSources: '/projects/prj_1/knowledge',
      syncPolicies: '/projects/prj_1/knowledge/sync-policies',
    });
  });
});
