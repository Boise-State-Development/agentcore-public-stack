import { describe, it, expect } from 'vitest';

import {
  AGENT_TEMPLATES,
  AGENT_TEMPLATE_DRAFT_KEY,
  findTemplate,
} from './agent-templates';

/**
 * Templates are DATA that flows through the form's population path unchanged, so the
 * checks worth pinning are shape + "is it actually finished" — a template with bracket
 * placeholders or an empty prompt would save a broken agent.
 */
describe('agent templates', () => {
  it('exposes the contract key the picker and form share', () => {
    expect(AGENT_TEMPLATE_DRAFT_KEY).toBe('agentTemplateDraft');
  });

  it('ships Course Helper as the must-have first template', () => {
    expect(AGENT_TEMPLATES[0]?.draft.templateId).toBe('course-helper');
    expect(findTemplate('course-helper')).toBeDefined();
  });

  describe('Course Helper', () => {
    const draft = findTemplate('course-helper')!;

    it('is fully written — no bracket placeholders left in author-facing text', () => {
      const authored = [draft.name, draft.description, draft.instructions, ...draft.starters].join(
        '\n',
      );
      // Catches [placeholder], {{mustache}}, and <angle> stubs.
      expect(authored).not.toMatch(/\[[^\]]+\]|\{\{[^}]+\}\}|<[a-z_ ]+>/i);
    });

    it('bakes the integrity + stay-on-material guardrails into the prompt', () => {
      const lower = draft.instructions.toLowerCase();
      expect(lower).toContain('academic integrity');
      expect(lower).toContain('graded work');
      // Stays grounded in the course materials.
      expect(lower).toContain('course materials');
    });

    it('leaves the model on the platform default', () => {
      expect(draft.modelConfig.modelId).toBeNull();
    });

    it('uses only valid binding kinds', () => {
      const kinds = new Set(['tool', 'skill', 'memory_space', 'knowledge_base']);
      for (const b of draft.bindings) {
        expect(kinds.has(b.kind)).toBe(true);
        expect(typeof b.ref).toBe('string');
        expect(b.ref.length).toBeGreaterThan(0);
      }
    });

    it('starts PRIVATE — an author publishes deliberately, never by default', () => {
      expect(draft.visibility).toBe('PRIVATE');
    });

    it('offers conversation starters', () => {
      expect(draft.starters.length).toBeGreaterThan(0);
    });
  });

  it('every catalog entry has a unique id and a picker pitch', () => {
    const ids = AGENT_TEMPLATES.map((e) => e.draft.templateId);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of AGENT_TEMPLATES) {
      expect(entry.pitch.length).toBeGreaterThan(0);
    }
  });
});
