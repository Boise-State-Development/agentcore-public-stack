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

    it('pins a concrete default model from the catalog', () => {
      expect(typeof draft.modelConfig.modelId).toBe('string');
      expect((draft.modelConfig.modelId as string).length).toBeGreaterThan(0);
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

/**
 * Catalog-wide coverage. Every published template (not just Course Helper) must be a
 * finished, save-able draft — no bracket stubs, valid binding kinds, PRIVATE by default —
 * and the three real starters plus the tool-notice demo must all be registered.
 */
describe('agent templates — full catalog', () => {
  const BRACKET_STUB = /\[[^\]]+\]|\{\{[^}]+\}\}|<[a-z_ ]+>/i;
  const VALID_KINDS = new Set(['tool', 'skill', 'memory_space', 'knowledge_base']);

  it('registers the three real starters and the tool-notice demo', () => {
    const ids = AGENT_TEMPLATES.map((e) => e.draft.templateId);
    expect(ids).toContain('course-helper');
    expect(ids).toContain('qa-helper');
    expect(ids).toContain('roleplay-tutor');
    expect(ids).toContain('demo-notice');
    expect(findTemplate('qa-helper')).toBeDefined();
    expect(findTemplate('roleplay-tutor')).toBeDefined();
    expect(findTemplate('demo-notice')).toBeDefined();
  });

  for (const entry of AGENT_TEMPLATES) {
    describe(entry.draft.templateId, () => {
      const draft = entry.draft;

      it('is fully written — no bracket placeholders in author-facing text', () => {
        const authored = [draft.name, draft.description, draft.instructions, ...draft.starters].join(
          '\n',
        );
        expect(authored).not.toMatch(BRACKET_STUB);
      });

      it('has a non-empty prompt and at least one starter', () => {
        expect(draft.instructions.trim().length).toBeGreaterThan(20);
        expect(draft.starters.length).toBeGreaterThan(0);
      });

      it('uses only valid binding kinds with non-empty refs', () => {
        for (const b of draft.bindings) {
          expect(VALID_KINDS.has(b.kind)).toBe(true);
          expect(typeof b.ref).toBe('string');
          expect(b.ref.length).toBeGreaterThan(0);
        }
      });

      it('starts PRIVATE and pins a concrete default model', () => {
        expect(draft.visibility).toBe('PRIVATE');
        expect(typeof draft.modelConfig.modelId).toBe('string');
        expect((draft.modelConfig.modelId as string).length).toBeGreaterThan(0);
      });
    });
  }

  it('the demo template carries a deliberately-unknown tool ref to fire the dropped notice', () => {
    const demo = findTemplate('demo-notice')!;
    const toolRefs = demo.bindings.filter((b) => b.kind === 'tool').map((b) => b.ref);
    expect(toolRefs).toContain('this_tool_does_not_exist_demo_only');
    // ...and still asks for a real active tool so the form populates a normal binding too.
    expect(toolRefs).toContain('gateway_search_boise_state');
  });
});
