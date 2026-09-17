// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';

import { TemplatePickerDialogComponent } from './template-picker-dialog.component';
import { AgentTemplatesService } from '../services/agent-templates.service';
import {
  AGENT_TEMPLATE_DRAFT_KEY,
  TemplateCatalogEntry,
  TemplateDraft,
} from '../agent-form/agent-templates';

/**
 * This dialog is one HALF of the template-prefill handoff — it loads the catalog from the
 * backend (`AgentTemplatesService`), writes the chosen draft, and navigates; the form's
 * `ngOnInit` reads it (the other half). The two never call each other, so the ONLY
 * contract that keeps them connected is (1) the exact localStorage key and (2) the exact
 * create route — both pinned here so a rename on this side can't silently break prefill.
 *
 * The template DATA is no longer imported statically (it moved to the backend), so the
 * service is stubbed via DI rather than the hardcoded array being asserted.
 */
describe('TemplatePickerDialogComponent', () => {
  let closed: number;
  let navigatedTo: unknown[][];

  const DRAFT_A: TemplateDraft = {
    templateId: 'document-qa',
    name: 'Document Q&A',
    description: 'Answers from your documents.',
    emoji: '📚',
    instructions: 'Answer strictly from the provided material.',
    tags: [],
    starters: ['What does the material say?'],
    modelConfig: { modelId: null, params: {} },
    bindings: [],
  };
  const CATALOG: TemplateCatalogEntry[] = [
    { draft: DRAFT_A, pitch: 'Answers strictly from the documents you give it.' },
  ];

  /**
   * Build the component with a stubbed templates service. `loadResult` controls what the
   * service returns: a catalog array (success/empty) or a rejected promise (error). The
   * fixture is returned so tests can `detectChanges()` (fires ngOnInit) and await the load.
   */
  function build(
    loadResult: TemplateCatalogEntry[] | Promise<never> = CATALOG,
  ): ComponentFixture<TemplatePickerDialogComponent> {
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
        {
          provide: AgentTemplatesService,
          useValue: {
            loadTemplates: () =>
              loadResult instanceof Promise ? loadResult : Promise.resolve(loadResult),
          },
        },
      ],
    });
    return TestBed.createComponent(TemplatePickerDialogComponent);
  }

  /** Fire ngOnInit and let the load promise (then → finally chain) settle. */
  async function init(
    fixture: ComponentFixture<TemplatePickerDialogComponent>,
  ): Promise<void> {
    fixture.detectChanges(); // ngOnInit → load()
    // A macrotask tick drains the promise's then/finally microtasks; whenStable alone
    // does not wait for a raw Promise the way it waits for zone-tracked async.
    await new Promise((resolve) => setTimeout(resolve));
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(() => localStorage.clear());

  it('loads the catalog from the service and lists it', async () => {
    const fixture = build(CATALOG);
    await init(fixture);
    const component = fixture.componentInstance;

    expect(component.loading()).toBe(false);
    expect(component.loadError()).toBe(false);
    expect(component.templates()).toEqual(CATALOG);
  });

  it('shows the empty state (no templates) without erroring', async () => {
    const fixture = build([]);
    await init(fixture);
    const component = fixture.componentInstance;

    expect(component.loading()).toBe(false);
    expect(component.loadError()).toBe(false);
    expect(component.templates()).toEqual([]);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'No templates are available yet',
    );
  });

  it('shows the error state when the load rejects', async () => {
    const fixture = build(Promise.reject(new Error('boom')));
    await init(fixture);
    const component = fixture.componentInstance;

    expect(component.loading()).toBe(false);
    expect(component.loadError()).toBe(true);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      "couldn't load the templates",
    );
  });

  it('writes the selected template under the exact key the form reads', () => {
    const component = build().componentInstance;

    component.onSelect(DRAFT_A);

    // The key MUST be this literal — it is the whole contract with the form.
    expect(AGENT_TEMPLATE_DRAFT_KEY).toBe('agentTemplateDraft');
    const stored = localStorage.getItem('agentTemplateDraft');
    expect(stored).not.toBeNull();
    expect(JSON.parse(stored as string)).toEqual(DRAFT_A);
  });

  it('navigates to the create-agent form route (create mode, no id)', () => {
    const component = build().componentInstance;

    component.onSelect(DRAFT_A);

    expect(navigatedTo).toEqual([['/agents/new']]);
  });

  it('closes the dialog when a template is chosen', () => {
    const component = build().componentInstance;

    component.onSelect(DRAFT_A);

    expect(closed).toBe(1);
  });

  it('still opens the builder if localStorage throws, rather than stranding the click', () => {
    const component = build().componentInstance;
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError');
    });

    component.onSelect(DRAFT_A);

    expect(navigatedTo).toEqual([['/agents/new']]);
    expect(closed).toBe(1);
    spy.mockRestore();
  });

  it('closes without writing or navigating on dismiss', () => {
    const component = build().componentInstance;

    component.onClose();

    expect(closed).toBe(1);
    expect(navigatedTo).toEqual([]);
    expect(localStorage.getItem('agentTemplateDraft')).toBeNull();
  });
});
