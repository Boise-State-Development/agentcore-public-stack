import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ConfigService } from '../../../services/config.service';
import { TaskModelCardComponent } from './task-model-card.component';
import { TaskModelSlot } from '../models/task-model.model';

const URL = 'http://localhost:8000/admin/task-models/session_title';

function task(overrides: Partial<TaskModelSlot> = {}): TaskModelSlot {
  return {
    slot: 'session_title',
    title: 'Conversation titles',
    description: 'Names each new conversation.',
    guidance: 'Pick a fast, low-cost model.',
    metered: false,
    available: true,
    defaultSource: 'code',
    defaultModel: { modelId: 'us.amazon.nova-micro-v1:0', modelName: null },
    defaultNotice: "The default isn't in the model catalog.",
    assignment: null,
    assignmentProblem: null,
    effective: { modelId: 'us.amazon.nova-micro-v1:0', modelName: null, effort: null, source: 'default' },
    eligibleModels: [
      {
        modelId: 'haiku',
        modelName: 'Claude Haiku 5.5',
        providerName: 'Anthropic',
        effortLevels: ['low', 'medium', 'high'],
        effortDefault: 'medium',
        inputPricePerMillionTokens: 0.11,
        outputPricePerMillionTokens: 0.55,
      },
      {
        modelId: 'glm',
        modelName: 'GLM 5',
        providerName: 'Z.ai',
        effortLevels: [],
        effortDefault: null,
        inputPricePerMillionTokens: 1,
        outputPricePerMillionTokens: 3.2,
      },
    ],
    ...overrides,
  };
}

describe('TaskModelCardComponent', () => {
  let fixture: ComponentFixture<TaskModelCardComponent>;
  let httpMock: HttpTestingController;
  let el: HTMLElement;

  const select = (id: string) => el.querySelector<HTMLSelectElement>(`#${id}`)!;
  const choose = async (id: string, value: string) => {
    const s = select(id);
    s.value = value;
    s.dispatchEvent(new Event('change'));
    await fixture.whenStable();
  };
  const button = (label: string) =>
    [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === label)!;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(TaskModelCardComponent);
    fixture.componentRef.setInput('task', task());
    el = fixture.nativeElement;
    // The shared service loads the list when it is created; the card renders from its input.
    await vi.waitFor(() =>
      httpMock.expectOne('http://localhost:8000/admin/task-models').flush({ slots: [] }),
    );
    await fixture.whenStable();
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => req.cancelled || req.flush({}));
    TestBed.resetTestingModule();
  });

  it('shows what the task does, what it runs on, and the retirement notice', () => {
    expect(el.textContent).toContain('Names each new conversation.');
    expect(el.textContent).toContain('Runs on us.amazon.nova-micro-v1:0');
    expect(el.textContent).toContain("The default isn't in the model catalog.");
    expect(el.textContent).toContain('Not metered');
  });

  it('offers the default first, then the eligible models with prices', () => {
    const options = [...select('task-model-session_title').options].map(o => o.text.trim());
    expect(options[0]).toBe('Default: us.amazon.nova-micro-v1:0');
    expect(options[1]).toBe('Claude Haiku 5.5 · $0.11 in / $0.55 out per MTok');
  });

  it('offers effort only for a model that has it', async () => {
    expect(select('task-effort-session_title').disabled).toBe(true);

    await choose('task-model-session_title', 'haiku');
    const effort = select('task-effort-session_title');
    expect(effort.disabled).toBe(false);
    expect([...effort.options].map(o => o.text.trim())).toEqual([
      'Model default (Medium)',
      'Low',
      'Medium',
      'High',
    ]);

    await choose('task-model-session_title', 'glm');
    expect(select('task-effort-session_title').disabled).toBe(true);
  });

  it('saves the model and effort, and clears an effort the new model lacks', async () => {
    await choose('task-model-session_title', 'haiku');
    await choose('task-effort-session_title', 'low');
    await choose('task-model-session_title', 'glm');
    await choose('task-model-session_title', 'haiku');
    await choose('task-effort-session_title', 'low');

    button('Save').click();
    const req = httpMock.expectOne(URL);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ modelId: 'haiku', effort: 'low' });
    req.flush(
      task({
        assignment: { modelId: 'haiku', modelName: 'Claude Haiku 5.5', effort: 'low', updatedAt: null, updatedBy: 'a' },
        effective: { modelId: 'haiku', modelName: 'Claude Haiku 5.5', effort: 'low', source: 'assigned' },
      }),
    );
    await vi.waitFor(() => expect(el.textContent).toContain('Saved'));
  });

  it('choosing the default sends a null model, which clears the assignment', async () => {
    fixture.componentRef.setInput(
      'task',
      task({ assignment: { modelId: 'haiku', modelName: 'Claude Haiku 5.5', effort: 'low', updatedAt: null, updatedBy: null } }),
    );
    await fixture.whenStable();
    await choose('task-model-session_title', '');
    button('Save').click();
    expect(httpMock.expectOne(URL).request.body).toEqual({ modelId: null, effort: null });
  });

  it("shows the backend's reason when a save is refused", async () => {
    await choose('task-model-session_title', 'glm');
    button('Save').click();
    httpMock
      .expectOne(URL)
      .flush({ detail: "It's turned off in the model catalog." }, { status: 400, statusText: 'Bad Request' });
    await vi.waitFor(() =>
      expect(el.querySelector('[role="alert"]')?.textContent).toContain("It's turned off in the model catalog."),
    );
  });

  it('says so when the assignment can no longer run', async () => {
    fixture.componentRef.setInput('task', task({ assignmentProblem: "It's retired." }));
    await fixture.whenStable();
    expect(el.textContent).toContain("The assigned model can't run, so the default is running instead. It's retired.");
  });
});
