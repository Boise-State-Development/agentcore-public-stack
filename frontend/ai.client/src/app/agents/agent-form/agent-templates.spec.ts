import { describe, it, expect } from 'vitest';

import { AGENT_TEMPLATE_DRAFT_KEY } from './agent-templates';

/**
 * The template DATA moved to the backend catalog (`GET /templates/`), so this module now
 * holds only the shared TYPES and the localStorage key. The types are compile-time; the
 * one runtime contract left to pin is the key the picker writes and the create-agent form
 * reads — a rename on either side would silently break prefill, so it is asserted here.
 * (Catalog content — finished prompts, valid bindings, PRIVATE-by-default — is now the
 * backend seed's responsibility and is covered by the backend template tests.)
 */
describe('agent templates client contract', () => {
  it('exposes the exact localStorage key the picker and form share', () => {
    expect(AGENT_TEMPLATE_DRAFT_KEY).toBe('agentTemplateDraft');
  });
});
