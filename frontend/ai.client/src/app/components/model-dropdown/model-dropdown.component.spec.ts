import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal, computed } from '@angular/core';
import { Router } from '@angular/router';
import { ModelDropdownComponent } from './model-dropdown.component';
import { ModelService } from '../../session/services/model/model.service';
import { SessionService } from '../../session/services/session/session.service';
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
  };

  const sessionService = {
    hasCurrentSession: () => options.hasSession ?? false,
  };

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: ModelService, useValue: modelService },
      { provide: SessionService, useValue: sessionService },
      { provide: Router, useValue: { navigate: vi.fn() } },
    ],
  });

  const fixture = TestBed.createComponent(ModelDropdownComponent);
  fixture.detectChanges();
  return { fixture, modelService, setSelectedModel, setEffort };
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

    // Name + provider share one line; the description is its own.
    const lines = row.querySelectorAll('.block');
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
    expect(row.querySelectorAll('.block').length).toBe(1);
  });

  it('hides the bullet separator from assistive tech', () => {
    const { fixture } = setup({
      featured: [makeModel({ modelName: 'Alpha', providerName: 'Anthropic' })],
    });
    openMenu(fixture);
    const bullet = itemLabelled('Alpha')!.querySelector('[aria-hidden="true"]');
    expect(bullet?.textContent?.trim()).toBe('\u2022');
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

  it('writes the chosen effort level back to the service', () => {
    const { fixture, setEffort } = setup({
      effortControl: { key: 'effort', levels: ['low', 'high'], defaultLevel: 'low' },
      selectedEffort: 'low',
    });
    openMenu(fixture);
    itemLabelled('Effort')!.click();
    fixture.detectChanges();
    itemLabelled('High')!.click();

    expect(setEffort).toHaveBeenCalledWith('high');
  });

  it('locks the picker to a plain label when an agent pins the model', () => {
    const { fixture, modelService } = setup();
    modelService.agentModelLocked.set(true);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('button[aria-label="Select model"]')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Model One');
  });
});
