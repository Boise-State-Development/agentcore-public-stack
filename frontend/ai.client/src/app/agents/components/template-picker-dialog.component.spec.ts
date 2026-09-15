// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';

import { TemplatePickerDialogComponent } from './template-picker-dialog.component';
import {
  AGENT_TEMPLATES,
  AGENT_TEMPLATE_DRAFT_KEY,
} from '../agent-form/agent-templates';

/**
 * This dialog is one HALF of the template-prefill handoff — it writes and navigates; the
 * form's `ngOnInit` reads (Phase 2). The two never call each other, so the ONLY contract
 * that keeps them connected is (1) the exact localStorage key and (2) the exact create
 * route. Both are pinned here so a rename on this side can't silently break Phase 2.
 *
 * DI tokens rather than vi.mock, per project convention (a shared worker pool leaks
 * module mocks across specs).
 */
describe('TemplatePickerDialogComponent', () => {
  let closed: number;
  let navigatedTo: unknown[][];

  function build(): TemplatePickerDialogComponent {
    closed = 0;
    navigatedTo = [];
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: DialogRef, useValue: { close: () => (closed += 1) } },
        {
          provide: Router,
          useValue: {
            navigate: (commands: unknown[]) => {
              navigatedTo.push(commands);
              return Promise.resolve(true);
            },
          },
        },
      ],
    });
    return TestBed.createComponent(TemplatePickerDialogComponent).componentInstance;
  }

  beforeEach(() => localStorage.clear());

  it('lists every catalog template', () => {
    expect(build().templates).toBe(AGENT_TEMPLATES);
    expect(AGENT_TEMPLATES.length).toBeGreaterThan(0);
  });

  it('writes the selected template under the exact key Phase 2 reads', () => {
    const component = build();
    const draft = AGENT_TEMPLATES[0].draft;

    component.onSelect(draft);

    // The key MUST be this literal — it is the whole contract with the form.
    expect(AGENT_TEMPLATE_DRAFT_KEY).toBe('agentTemplateDraft');
    const stored = localStorage.getItem('agentTemplateDraft');
    expect(stored).not.toBeNull();
    expect(JSON.parse(stored as string)).toEqual(draft);
  });

  it('navigates to the create-agent form route (create mode, no id)', () => {
    const component = build();

    component.onSelect(AGENT_TEMPLATES[0].draft);

    expect(navigatedTo).toEqual([['/agents/new']]);
  });

  it('closes the dialog when a template is chosen', () => {
    const component = build();

    component.onSelect(AGENT_TEMPLATES[0].draft);

    expect(closed).toBe(1);
  });

  it('still opens the builder if localStorage throws, rather than stranding the click', () => {
    const component = build();
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError');
    });

    component.onSelect(AGENT_TEMPLATES[0].draft);

    expect(navigatedTo).toEqual([['/agents/new']]);
    expect(closed).toBe(1);
    spy.mockRestore();
  });

  it('closes without writing or navigating on dismiss', () => {
    const component = build();

    component.onClose();

    expect(closed).toBe(1);
    expect(navigatedTo).toEqual([]);
    expect(localStorage.getItem('agentTemplateDraft')).toBeNull();
  });
});
