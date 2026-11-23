import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { GraphEdge, GraphStepCard, WorkflowGraphComponent } from './workflow-graph.component';

const CARD: GraphStepCard = { key: 'a', title: 'Generalist', meta: 'Agent', agentId: null, readsLabel: 'reads the request' };

/** Two graphs on one page, as a run's row and the sheet's small graph can be. */
@Component({
  imports: [WorkflowGraphComponent],
  template: `
    @for (label of labels; track $index) {
      <app-workflow-graph
        [stages]="stages"
        [edges]="edges"
        [start]="{ title: 'Starts with', meta: 'A request' }"
        [result]="{ title: 'One card', meta: 'Back to chat' }"
        [stepCount]="1"
        [maxSteps]="8"
        [mode]="mode"
        [label]="label"
      />
    }
  `,
})
class TwoGraphs {
  labels: (string | null)[] = [null, null];
  mode: 'design' | 'run' = 'design';
  stages: GraphStepCard[][] = [[CARD], [{ ...CARD, key: 'b', title: 'Summary' }]];
  readonly edges: GraphEdge[] = [['start', 'a'], ['a', 'b'], ['b', 'result']];
}

function render(setup: (host: TwoGraphs) => void = () => undefined): HTMLElement {
  const fixture = TestBed.createComponent(TwoGraphs);
  setup(fixture.componentInstance);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('WorkflowGraphComponent', () => {
  it('gives each instance its own ids, so every heading and description it names is its own', () => {
    const el = render();
    const ids = [...el.querySelectorAll('[id]')].map((node) => node.id);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);

    for (const graph of el.querySelectorAll('app-workflow-graph')) {
      for (const node of graph.querySelectorAll('[aria-labelledby], [aria-describedby]')) {
        const refs = `${node.getAttribute('aria-labelledby') ?? ''} ${node.getAttribute('aria-describedby') ?? ''}`.trim().split(/\s+/);
        for (const ref of refs) expect(graph.querySelector(`#${ref}`), ref).not.toBeNull();
      }
    }
    const headings = [...el.querySelectorAll('app-workflow-graph')].map((g) =>
      g.querySelector(`#${g.querySelector('[role="group"]')!.getAttribute('aria-labelledby')}`)!.textContent!.trim(),
    );
    expect(headings).toEqual(['Stage 1', 'Stage 1']);
  });

  it('says a stage runs "4 at once" only for its Agent steps, which a prompt step never takes a slot from', () => {
    const five = (kind?: 'prompt'): GraphStepCard[] =>
      Array.from({ length: 5 }, (_, i) => ({ ...CARD, key: `s${i}`, title: `Step ${i}`, ...(kind ? { kind } : {}) }));
    const heading = (el: HTMLElement): string =>
      el.querySelector(`#${el.querySelector('[role="group"]')!.getAttribute('aria-labelledby')}`)!.textContent!.replace(/\s+/g, ' ').trim();

    expect(heading(render((host) => (host.stages = [five()])))).toBe('Stage 1 · 4 at once');
    expect(heading(render((host) => (host.stages = [five('prompt')])))).toBe('Stage 1');
    // Five Agent steps beside prompt steps still share four slots.
    expect(heading(render((host) => (host.stages = [[...five(), ...five('prompt').map((c) => ({ ...c, key: `p${c.key}` }))]])))).toBe(
      'Stage 1 · 4 at once',
    );
  });

  it('names its scroll region from the label input, else by its mode', () => {
    const named = render((host) => {
      host.labels = ['Graph of this run', null];
      host.mode = 'run';
    });
    expect([...named.querySelectorAll('[role="region"]')].map((r) => r.getAttribute('aria-label'))).toEqual([
      'Graph of this run',
      'Test run canvas',
    ]);
  });
});
