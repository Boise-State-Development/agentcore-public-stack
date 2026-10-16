import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal, computed } from '@angular/core';
import { ModelDropdownComponent } from './model-dropdown.component';
import { ModelService } from '../../session/services/model/model.service';
import { SessionService } from '../../session/services/session/session.service';
import { PromptCacheGuardService } from '../../session/services/prompt-cache/prompt-cache-guard.service';
import {
  EffortControl,
  ManagedModel,
} from '../../admin/manage-models/models/managed-model.model';

function makeModel(overrides: Partial<ManagedModel> = {}): ManagedModel {
  return {
    id: 'm1',
    modelId: 'model-1',
    modelName: 'Model One',
    provider: 'bedrock',
    providerName: 'Anthropic',
    inputModalities: ['TEXT'],
    outputModalities: ['TEXT'],
    maxInputTokens: 200000,
    maxOutputTokens: 4096,
    allowedAppRoles: [],
    availableToRoles: [],
    enabled: true,
    inputPricePerMillionTokens: 1,
    outputPricePerMillionTokens: 1,
    knowledgeCutoffDate: null,
    supportsCaching: true,
    isDefault: false,
    ...overrides,
  };
}

/**
 * Stand-in for ModelService exposing exactly the signals the picker reads.
 * DI-token override rather than vi.mock, per house convention.
 */
function setup(options: {
  featured?: ManagedModel[];
  more?: ManagedModel[];
  selected?: ManagedModel;
  effortControl?: EffortControl | null;
  selectedEffort?: string | null;
  hasSession?: boolean;
  /** What the prompt-cache guard answers when asked. */
  confirm?: boolean;
} = {}) {
  const featured = signal(options.featured ?? [makeModel()]);
  const more = signal(options.more ?? []);
  const selected = signal(options.selected ?? featured()[0]);
  const effortControl = signal<EffortControl | null>(options.effortControl ?? null);
  const selectedEffort = signal<string | null>(options.selectedEffort ?? null);
  const setSelectedModel = vi.fn();
  const setEffort = vi.fn();

  const modelService = {
    featuredModels: featured,
    moreModels: more,
    selectedModel: selected,
    availableModels: computed(() => [...featured(), ...more()]),
    modelsLoading: signal(false),
    modelsError: signal(null),
    agentModelLocked: signal(false),
    effortControl,
    selectedEffort,
    setSelectedModel,
    setEffort,
    modelNameFor: vi.fn(() => null),
  };

  const sessionService = {
    hasCurrentSession: () => options.hasSession ?? false,
  };
  const confirm = vi.fn(async () => options.confirm ?? true);

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: ModelService, useValue: modelService },
      { provide: SessionService, useValue: sessionService },
      { provide: PromptCacheGuardService, useValue: { confirm } },
    ],
  });

  const fixture = TestBed.createComponent(ModelDropdownComponent);
  fixture.detectChanges();
  return { fixture, modelService, setSelectedModel, setEffort, confirm };
}

/** Open the picker by clicking its trigger, then flush the overlay render. */
function openMenu(fixture: ReturnType<typeof setup>['fixture']) {
  const trigger = fixture.nativeElement.querySelector('button[aria-label="Select model"]');
  expect(trigger, 'trigger button should render').toBeTruthy();
  trigger.click();
  fixture.detectChanges();
}

/** CDK renders menus into an overlay container outside the fixture element. */
function menuItems(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="menuitem"]'));
}

function itemLabelled(text: string): HTMLElement | undefined {
  return menuItems().find(el => el.textContent?.includes(text));
}

describe('ModelDropdownComponent', () => {
  beforeEach(() => {
    document.querySelectorAll('.cdk-overlay-container').forEach(el => el.remove());
  });

  it('renders one menu item per featured model', () => {
    const { fixture } = setup({
      featured: [
        makeModel({ id: 'a', modelId: 'a', modelName: 'Alpha' }),
        makeModel({ id: 'b', modelId: 'b', modelName: 'Beta' }),
      ],
    });
    openMenu(fixture);

    // The rows are a child component carrying `cdkMenuItem` on its host. If
    // that ever regresses to an ngTemplateOutlet, CDK's CONTENT query stops
    // matching them and this count drops to zero while the rows still render
    // — which is the whole reason the row is a component.
    expect(itemLabelled('Alpha')).toBeTruthy();
    expect(itemLabelled('Beta')).toBeTruthy();
  });

  it('puts the provider on the name line and the description below it', () => {
    const { fixture } = setup({
      featured: [
        makeModel({
          modelName: 'Alpha',
          providerName: 'Anthropic',
          shortDescription: 'For hard problems',
        }),
      ],
    });
    openMenu(fixture);
    const row = itemLabelled('Alpha')!;
    expect(row.textContent).toContain('Anthropic');
    expect(row.textContent).toContain('For hard problems');

    // Name + provider share one line; the description is its own. Scoped to the
    // text wrapper: the leading avatar's light/dark <img> pair also carries
    // `block`, and it is not a line of text.
    const lines = row.querySelector('.text-left')!.querySelectorAll('.block');
    expect(lines.length).toBe(2);
    expect(lines[0].textContent).toContain('Alpha');
    expect(lines[0].textContent).toContain('Anthropic');
    expect(lines[1].textContent).toContain('For hard problems');
  });

  it('collapses to a single line when a model has no description', () => {
    // This is most of what makes the menu shorter — an undescribed model must
    // not leave an empty second line behind.
    const { fixture } = setup({
      featured: [makeModel({ modelName: 'Alpha', providerName: 'Anthropic' })],
    });
    openMenu(fixture);
    const row = itemLabelled('Alpha')!;
    expect(row.textContent).toContain('Anthropic');
    expect(row.querySelector('.text-left')!.querySelectorAll('.block').length).toBe(1);
  });

  it('hides the bullet separator from assistive tech', () => {
    const { fixture } = setup({
      featured: [makeModel({ modelName: 'Alpha', providerName: 'Anthropic' })],
    });
    openMenu(fixture);
    // Scoped past the leading avatar, which is aria-hidden for the same reason.
    const bullet = itemLabelled('Alpha')!
      .querySelector('.text-left')!
      .querySelector('[aria-hidden="true"]');
    expect(bullet?.textContent?.trim()).toBe('\u2022');
  });

  it('leads each row with the model\'s vendor avatar', () => {
    // Left-aligned identity: the picker is scanned, not read, and a logo lands
    // before the name does.
    const { fixture } = setup({
      featured: [makeModel({ modelName: 'Alpha', providerName: 'Anthropic' })],
    });
    openMenu(fixture);
    const logo = itemLabelled('Alpha')!.querySelector('img');
    expect(logo?.getAttribute('src')).toBe('/img/provider-logos/anthropic/light.svg');
  });

  it('omits the "More models" entry when nothing is demoted', () => {
    const { fixture } = setup({ featured: [makeModel()], more: [] });
    openMenu(fixture);
    expect(itemLabelled('More models')).toBeUndefined();
  });

  it('offers a "More models" entry when models are demoted', () => {
    const { fixture } = setup({
      featured: [makeModel({ id: 'a', modelId: 'a', modelName: 'Alpha' })],
      more: [makeModel({ id: 'z', modelId: 'z', modelName: 'Zeta', isFeatured: false })],
    });
    openMenu(fixture);

    // Demoted models stay out of the top level until the submenu is opened.
    expect(itemLabelled('Zeta')).toBeUndefined();
    expect(itemLabelled('More models')).toBeTruthy();
  });

  it('reveals demoted models when the submenu is opened', () => {
    const { fixture } = setup({
      featured: [makeModel({ id: 'a', modelId: 'a', modelName: 'Alpha' })],
      more: [makeModel({ id: 'z', modelId: 'z', modelName: 'Zeta', isFeatured: false })],
    });
    openMenu(fixture);
    itemLabelled('More models')!.click();
    fixture.detectChanges();

    expect(itemLabelled('Zeta')).toBeTruthy();
  });

  it('omits the Effort entry for a model with no effort control', () => {
    const { fixture } = setup({ effortControl: null });
    openMenu(fixture);
    expect(itemLabelled('Effort')).toBeUndefined();
  });

  it('offers an Effort entry showing the active level', () => {
    const { fixture } = setup({
      effortControl: { key: 'effort', levels: ['low', 'medium', 'high'], defaultLevel: 'medium' },
      selectedEffort: 'medium',
    });
    openMenu(fixture);

    const effortItem = itemLabelled('Effort');
    expect(effortItem).toBeTruthy();
    expect(effortItem?.textContent).toContain('Medium');
  });

  it('surfaces the active effort in the trigger without opening the menu', () => {
    const { fixture } = setup({
      effortControl: { key: 'effort', levels: ['low', 'high'], defaultLevel: 'low' },
      selectedEffort: 'high',
    });
    const trigger = fixture.nativeElement.querySelector('button[aria-label="Select model"]');
    expect(trigger.textContent).toContain('High');
  });

  it('lists the model-declared effort levels and marks the admin default', () => {
    const { fixture } = setup({
      effortControl: { key: 'effort', levels: ['low', 'medium', 'high'], defaultLevel: 'medium' },
      selectedEffort: 'high',
    });
    openMenu(fixture);
    itemLabelled('Effort')!.click();
    fixture.detectChanges();

    for (const label of ['Low', 'Medium', 'High']) {
      expect(itemLabelled(label), `${label} should be offered`).toBeTruthy();
    }
    expect(itemLabelled('Medium')?.textContent).toContain('Default');
  });

  it('writes the chosen effort level back to the service', async () => {
    const { fixture, setEffort, confirm } = setup({
      effortControl: { key: 'effort', levels: ['low', 'high'], defaultLevel: 'low' },
      selectedEffort: 'low',
    });
    openMenu(fixture);
    itemLabelled('Effort')!.click();
    fixture.detectChanges();
    itemLabelled('High')!.click();

    await vi.waitFor(() => expect(setEffort).toHaveBeenCalledWith('high'));
    // No conversation yet, so there is no cache to lose.
    expect(confirm).not.toHaveBeenCalled();
  });

  describe('changing effort in an open conversation', () => {
    const effort = { key: 'effort', levels: ['low', 'high'], defaultLevel: 'low' };

    function pickEffort(fixture: ReturnType<typeof setup>['fixture'], label: string) {
      openMenu(fixture);
      itemLabelled('Effort')!.click();
      fixture.detectChanges();
      itemLabelled(label)!.click();
    }

    it('asks the prompt-cache guard, then applies the level', async () => {
      const { fixture, setEffort, confirm } = setup({ effortControl: effort, selectedEffort: 'low', hasSession: true });
      pickEffort(fixture, 'High');

      await vi.waitFor(() => expect(setEffort).toHaveBeenCalledWith('high'));
      expect(confirm).toHaveBeenCalledWith(
        expect.objectContaining({ kind: 'effort', fromLabel: 'Low', toLabel: 'High' }),
      );
    });

    it('keeps the current level when the user cancels', async () => {
      const { fixture, setEffort, confirm } = setup({
        effortControl: effort,
        selectedEffort: 'low',
        hasSession: true,
        confirm: false,
      });
      pickEffort(fixture, 'High');

      await vi.waitFor(() => expect(confirm).toHaveBeenCalled());
      await Promise.resolve();
      expect(setEffort).not.toHaveBeenCalled();
    });

    it('does not ask when re-picking the current level', async () => {
      const { fixture, setEffort, confirm } = setup({ effortControl: effort, selectedEffort: 'low', hasSession: true });
      pickEffort(fixture, 'Low');
      await Promise.resolve();

      expect(confirm).not.toHaveBeenCalled();
      expect(setEffort).not.toHaveBeenCalled();
    });
  });

  it('rotates the trigger chevron while the menu is open', () => {
    // The open state comes from the trigger's cdkMenuOpened/cdkMenuClosed
    // outputs. CdkMenu has no `opened` output, so an `(opened)` binding on the
    // panel compiles to a DOM listener that never fires and the chevron sticks.
    const { fixture } = setup();
    const trigger: HTMLButtonElement = fixture.nativeElement.querySelector(
      'button[aria-label="Select model"]',
    );
    const chevron = () => trigger.querySelector('svg')!;
    expect(chevron().classList).not.toContain('rotate-180');

    trigger.click();
    fixture.detectChanges();
    expect(menuItems().length).toBeGreaterThan(0);
    expect(chevron().classList).toContain('rotate-180');

    // A second click on the trigger toggles the menu closed.
    trigger.click();
    fixture.detectChanges();
    expect(menuItems().length).toBe(0);
    expect(chevron().classList).not.toContain('rotate-180');
  });

  it('un-rotates the chevron when choosing a model closes the menu', () => {
    const { fixture, setSelectedModel } = setup({
      featured: [
        makeModel({ id: 'a', modelId: 'a', modelName: 'Alpha' }),
        makeModel({ id: 'b', modelId: 'b', modelName: 'Beta' }),
      ],
    });
    const chevron = () =>
      fixture.nativeElement.querySelector('button[aria-label="Select model"] svg') as SVGElement;
    openMenu(fixture);
    expect(chevron().classList).toContain('rotate-180');

    itemLabelled('Beta')!.click();
    fixture.detectChanges();
    expect(setSelectedModel).toHaveBeenCalled();
    expect(chevron().classList).not.toContain('rotate-180');
  });

  it('locks the picker to a plain label when an agent pins the model', () => {
    const { fixture, modelService } = setup();
    modelService.agentModelLocked.set(true);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('button[aria-label="Select model"]')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Model One');
  });

  describe('switching models in an open conversation', () => {
    const twoModels = [
      makeModel({ id: 'a', modelId: 'a', modelName: 'Alpha' }),
      makeModel({ id: 'b', modelId: 'b', modelName: 'Beta' }),
    ];

    function pick(fixture: ReturnType<typeof setup>['fixture'], name: string) {
      openMenu(fixture);
      itemLabelled(name)!.click();
      fixture.detectChanges();
    }

    it('continues the conversation on the new model once the guard confirms', async () => {
      const { fixture, setSelectedModel, confirm } = setup({ featured: twoModels, hasSession: true });
      pick(fixture, 'Beta');

      await vi.waitFor(() => expect(setSelectedModel).toHaveBeenCalledWith(twoModels[1]));
      expect(confirm).toHaveBeenCalledWith({ kind: 'model', from: twoModels[0], to: twoModels[1] });
    });

    it('keeps the current model when the user cancels', async () => {
      const { fixture, setSelectedModel, confirm } = setup({ featured: twoModels, hasSession: true, confirm: false });
      pick(fixture, 'Beta');

      await vi.waitFor(() => expect(confirm).toHaveBeenCalled());
      await Promise.resolve();
      expect(setSelectedModel).not.toHaveBeenCalled();
    });

    it('does nothing when re-picking the current model', async () => {
      const { fixture, setSelectedModel, confirm } = setup({ featured: twoModels, hasSession: true });
      pick(fixture, 'Alpha');
      await Promise.resolve();

      expect(confirm).not.toHaveBeenCalled();
      expect(setSelectedModel).not.toHaveBeenCalled();
    });

    it('changes the model without asking on a fresh composer', async () => {
      const { fixture, setSelectedModel, confirm } = setup({ featured: twoModels });
      pick(fixture, 'Beta');

      await vi.waitFor(() => expect(setSelectedModel).toHaveBeenCalledWith(twoModels[1]));
      expect(confirm).not.toHaveBeenCalled();
    });

    it('no longer labels another model as starting a new conversation', () => {
      const { fixture } = setup({ featured: twoModels, hasSession: true });
      openMenu(fixture);

      expect(document.querySelector('[role="menu"]')!.textContent).not.toContain('starts a new');
      expect(itemLabelled('Beta')!.textContent).not.toContain('New');
    });
  });
});
