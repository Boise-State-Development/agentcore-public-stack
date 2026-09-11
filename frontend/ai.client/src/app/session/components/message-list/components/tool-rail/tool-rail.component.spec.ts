import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { ToolRailComponent } from './tool-rail.component';
import { ToolCallGroup, ToolCallDisplay } from './tool-rail.model';

function makeCall(overrides: Partial<ToolCallDisplay> = {}): ToolCallDisplay {
  return {
    id: 'tool-1',
    toolName: 'search_classes',
    input: { query: 'CS 101' },
    status: 'complete',
    ...overrides,
  };
}

function makeGroup(overrides: Partial<ToolCallGroup> = {}): ToolCallGroup {
  return {
    calls: [makeCall()],
    ...overrides,
  };
}

describe('ToolRailComponent', () => {
  let fixture: ComponentFixture<ToolRailComponent>;
  let component: ToolRailComponent;

  beforeEach(async () => {
    TestBed.resetTestingModule();

    await TestBed.configureTestingModule({
      imports: [ToolRailComponent],
    })
      // The component's own templateUrl/styleUrl are not resolvable in this
      // test runner, so the structure under test is mirrored inline. Kept
      // deliberately thinner than the real template: only the parts these
      // assertions touch, so a purely presentational change to the real
      // markup does not fail this spec.
      .overrideComponent(ToolRailComponent, {
        set: {
          templateUrl: undefined as never,
          styleUrl: undefined as never,
          styles: [],
          template: `
            <button type="button" (click)="toggleExpanded()" [attr.aria-expanded]="isExpanded()">
              <span [class.text-shimmer]="isRunning()">{{ headline() }}</span>
              @if (group().calls.length > 1) {
                <span class="tool-count">{{ group().calls.length }} tools</span>
              }
              @if (totalDurationMs(); as total) {
                <span class="total-duration">{{ formatDuration(total) }}</span>
              }
              @if (failureCount(); as failures) {
                <span class="failure-count">{{ failures }} failed</span>
              }
            </button>
            <div class="collapsible-content" [class.open]="isExpanded()">
              @for (batch of batches(); track batch.key) {
              @if (batch.summary) { <p class="batch-summary">{{ batch.summary }}</p> }
              @for (call of batch.calls; track call.id) {
                <div class="call-row">
                  <button type="button" class="call-toggle" (click)="toggleCallDetail(call.id)" [attr.aria-expanded]="isCallDetailOpen(call)">
                    <span [class]="statusDotClass(call)"></span>
                    <span class="call-description">{{ describe(call) }}</span>
                    <span class="call-tool-name">{{ call.toolName }}</span>
                    @if (call.durationMs) {
                      <span class="call-duration">{{ formatDuration(call.durationMs) }}</span>
                    }
                  </button>
                  @if (isCallDetailOpen(call)) {
                  @if (call.input && (call.input | keyvalue)?.length) {
                    <div class="call-input"><span>input:</span><span>{{ formatInput(call.input) }}</span></div>
                  }
                  @if (call.result) {
                    <div class="call-result">
                      @if (isResultExpanded(call.id)) {
                        @for (item of call.result.content; track $index) {
                          @if (item.text) { <div>{{ item.text }}</div> }
                        }
                      } @else {
                        <span>{{ truncateResult(getResultText(call)) }}</span>
                      }
                      @if (getResultText(call).length > 200) {
                        <button type="button" class="toggle-result" (click)="toggleFullResult(call.id)">
                          {{ isResultExpanded(call.id) ? 'Show less' : 'Show full result' }}
                        </button>
                      }
                    </div>
                    @for (item of getResultImages(call); track $index) {
                      <img [src]="getImageDataUrl(item)" alt="Tool result image" />
                    }
                  }
                  }
                </div>
              }
              }
            </div>
          `,
        },
      })
      .compileComponents();

    fixture = TestBed.createComponent(ToolRailComponent);
    component = fixture.componentInstance;
  });

  function render(group: ToolCallGroup) {
    fixture.componentRef.setInput('group', group);
    fixture.detectChanges();
  }

  describe('collapsed headline', () => {
    it('prefers the model-generated summary when one exists', () => {
      render(makeGroup({ groupSummary: 'Found 3 sections of CS 101' }));

      expect(component.headline()).toBe('Found 3 sections of CS 101');
      expect(fixture.nativeElement.querySelector('button').textContent).toContain(
        'Found 3 sections of CS 101',
      );
    });

    it('falls back to a described line, never to a raw tool name', () => {
      // The old fallback chained identifiers ("search_classes → list_terms →
      // get_section"), which is the unreadable state this rail exists to fix.
      render(
        makeGroup({
          calls: [
            makeCall({ id: 'a', toolName: 'search_classes' }),
            makeCall({ id: 'b', toolName: 'list_terms' }),
          ],
        }),
      );

      const headline = component.headline();
      expect(headline).not.toContain('search_classes');
      expect(headline).not.toContain('→');
      expect(headline.length).toBeGreaterThan(0);
    });

    it('does not grow with the size of the group', () => {
      const many = makeGroup({
        calls: Array.from({ length: 12 }, (_, i) =>
          makeCall({ id: `t${i}`, toolName: 'search_classes' }),
        ),
      });
      render(many);

      // The whole point of collapsing: 12 calls must not produce 12 names.
      expect(component.headline().length).toBeLessThan(80);
    });

    it('shows a tool count only for a multi-call group', () => {
      render(makeGroup());
      expect(fixture.nativeElement.querySelector('.tool-count')).toBeNull();

      render(
        makeGroup({
          calls: [makeCall({ id: 'a' }), makeCall({ id: 'b' })],
        }),
      );
      expect(
        fixture.nativeElement.querySelector('.tool-count').textContent,
      ).toContain('2 tools');
    });
  });

  describe('batches', () => {
    function batched(...specs: { summary?: string; names: string[] }[]) {
      let n = 0;
      const batches = specs.map((spec, b) => ({
        key: `b${b}`,
        summary: spec.summary,
        calls: spec.names.map(name => makeCall({ id: `c${n++}`, toolName: name })),
      }));
      return { calls: batches.flatMap(b => b.calls), batches };
    }

    it('renders one line per summarized batch', () => {
      // Regression: a rail collapsing a multi-round pipeline showed only the
      // FIRST batch's summary, so it claimed the whole group did what its
      // opening round did.
      render(
        batched(
          { summary: 'Found 3 courses', names: ['list_courses'] },
          { summary: 'Found the Syllabus assignment', names: ['list_assignments'] },
        ),
      );
      component.toggleExpanded();
      fixture.detectChanges();

      const lines = [...fixture.nativeElement.querySelectorAll('.batch-summary')].map(
        (el: Element) => el.textContent!.trim(),
      );
      expect(lines).toEqual(['Found 3 courses', 'Found the Syllabus assignment']);
    });

    it('keeps every call, under its own batch', () => {
      render(
        batched(
          { summary: 'Round one', names: ['list_courses', 'list_terms'] },
          { summary: 'Round two', names: ['get_course'] },
        ),
      );
      component.toggleExpanded();
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelectorAll('.call-row').length).toBe(3);
    });

    it('omits the line for an unsummarized batch', () => {
      render(batched({ names: ['list_courses'] }));
      component.toggleExpanded();
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.batch-summary')).toBeNull();
      expect(fixture.nativeElement.querySelector('.call-description')).toBeTruthy();
    });

    it('treats a group with no batches as one implicit batch', () => {
      // Older callers and tests omit `batches` entirely.
      render(makeGroup({ calls: [makeCall({ id: 'a' }), makeCall({ id: 'b' })] }));

      expect(component.batches().length).toBe(1);
      expect(component.batches()[0].calls.length).toBe(2);
    });

    describe('collapsed headline', () => {
      it('states the summary alone when it covers the whole group', () => {
        render(batched({ summary: 'Found 3 courses', names: ['list_courses'] }));
        expect(component.headline()).toBe('Found 3 courses');
      });

      it('says how many rounds follow rather than over-claiming', () => {
        render(
          batched(
            { summary: 'Found 3 courses', names: ['list_courses'] },
            { summary: 'Found the assignment', names: ['list_assignments'] },
            { summary: 'Read it', names: ['get_assignment_details'] },
          ),
        );
        expect(component.headline()).toBe('Found 3 courses, then 2 more steps');
      });

      it('uses the singular for a single following round', () => {
        render(
          batched(
            { summary: 'Found 3 courses', names: ['list_courses'] },
            { summary: 'Read it', names: ['get_course'] },
          ),
        );
        expect(component.headline()).toBe('Found 3 courses, then 1 more step');
      });

      it('does not grow with the size of the group', () => {
        // Only ever ONE summary in the header — that is the point of
        // collapsing.
        render(
          batched(
            ...Array.from({ length: 8 }, (_, i) => ({
              summary: `Round ${i} did a thing`,
              names: ['list_courses'],
            })),
          ),
        );
        expect(component.headline()).toBe('Round 0 did a thing, then 7 more steps');
      });

      it('counts only the rounds after the first summarized one', () => {
        render(
          batched(
            { names: ['list_courses'] },
            { summary: 'Found the assignment', names: ['list_assignments'] },
          ),
        );
        expect(component.headline()).toBe('Found the assignment');
      });

      it('falls back to the deterministic line when nothing was summarized', () => {
        render(batched({ names: ['list_assignments'] }));
        expect(component.headline()).toBe('Listed assignments');
      });

      it('an explicit groupSummary overrides the derived header', () => {
        render({
          ...batched({ summary: 'Derived', names: ['list_courses'] }),
          groupSummary: 'Explicit',
        });
        expect(component.headline()).toBe('Explicit');
      });
    });
  });

  describe('expand/collapse', () => {
    it('starts collapsed', () => {
      render(makeGroup());
      expect(component.isExpanded()).toBe(false);
    });

    it('stays collapsed while tools are still running', () => {
      // Regression: the rail used to force itself open on any pending call,
      // so it was at its tallest exactly while the user was watching it.
      render(
        makeGroup({
          calls: [
            makeCall({ id: 'a', status: 'complete' }),
            makeCall({ id: 'b', status: 'pending' }),
          ],
        }),
      );

      expect(component.isRunning()).toBe(true);
      expect(component.isExpanded()).toBe(false);
      expect(
        fixture.nativeElement.querySelector('.collapsible-content').classList,
      ).not.toContain('open');
    });

    it('shimmers the headline while running instead of expanding', () => {
      render(makeGroup({ calls: [makeCall({ status: 'pending' })] }));

      const label = fixture.nativeElement.querySelector('button span');
      expect(label.classList).toContain('text-shimmer');
    });

    it('toggles on click', () => {
      render(makeGroup());
      const button = fixture.nativeElement.querySelector('button');

      button.click();
      fixture.detectChanges();
      expect(component.isExpanded()).toBe(true);

      button.click();
      fixture.detectChanges();
      expect(component.isExpanded()).toBe(false);
    });

    it('exposes the collapsed state to assistive technology', () => {
      render(makeGroup());
      const button = fixture.nativeElement.querySelector('button');
      expect(button.getAttribute('aria-expanded')).toBe('false');

      component.toggleExpanded();
      fixture.detectChanges();
      expect(button.getAttribute('aria-expanded')).toBe('true');
    });
  });

  describe('expanded detail', () => {
    it('keeps the raw input and result, not just a restatement', () => {
      // The summary is what you read; this is what you check it against.
      render(
        makeGroup({
          groupSummary: 'Found 3 sections of CS 101',
          calls: [
            makeCall({
              result: { status: 'success', content: [{ text: 'CS 101 · 3 sections' }] },
            }),
          ],
        }),
      );
      component.toggleExpanded();
      component.toggleCallDetail('tool-1');
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.call-input').textContent).toContain(
        'query: "CS 101"',
      );
      expect(fixture.nativeElement.querySelector('.call-result').textContent).toContain(
        'CS 101 · 3 sections',
      );
    });

    it('leads each row with a human line and keeps the identifier beside it', () => {
      render(makeGroup({ calls: [makeCall({ toolName: 'list_assignments' })] }));
      component.toggleExpanded();
      fixture.detectChanges();

      expect(
        fixture.nativeElement.querySelector('.call-description').textContent.trim(),
      ).toBe('Listed assignments');
      // The identifier still matters — it is what an admin greps for.
      expect(
        fixture.nativeElement.querySelector('.call-tool-name').textContent.trim(),
      ).toBe('list_assignments');
    });

    it('prefers a per-call summary over the derived description', () => {
      render(
        makeGroup({ calls: [makeCall({ summary: 'Pulled the fall roster' })] }),
      );
      expect(component.describe(makeCall({ summary: 'Pulled the fall roster' }))).toBe(
        'Pulled the fall roster',
      );
    });

    it('does not render an input row for an empty input', () => {
      render(makeGroup({ calls: [makeCall({ input: {} })] }));
      component.toggleExpanded();
      component.toggleCallDetail('tool-1');
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.call-input')).toBeNull();
    });

    // Split into two cases on purpose: the detail is now a real `@if`, so a
    // second `toggle*` call in one case folds it back up and the assertion
    // silently measures the wrong state.
    it('offers no "show full result" for a short result', () => {
      render(
        makeGroup({
          calls: [makeCall({ result: { status: 'success', content: [{ text: 'short' }] } })],
        }),
      );
      component.toggleExpanded();
      component.toggleCallDetail('tool-1');
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.call-result')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('.toggle-result')).toBeNull();
    });

    it('offers "show full result" for a truncated one', () => {
      render(
        makeGroup({
          calls: [
            makeCall({
              result: { status: 'success', content: [{ text: 'A'.repeat(300) }] },
            }),
          ],
        }),
      );
      component.toggleExpanded();
      component.toggleCallDetail('tool-1');
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.toggle-result')).toBeTruthy();
    });

    it('tracks result expansion per call', () => {
      render(
        makeGroup({
          calls: [makeCall({ id: 'a' }), makeCall({ id: 'b' })],
        }),
      );

      component.toggleFullResult('a');
      expect(component.isResultExpanded('a')).toBe(true);
      expect(component.isResultExpanded('b')).toBe(false);
    });

    it('renders images from result content', () => {
      render(
        makeGroup({
          calls: [
            makeCall({
              result: {
                status: 'success',
                content: [{ image: { format: 'png', data: 'abc' } }],
              },
            }),
          ],
        }),
      );
      component.toggleExpanded();
      component.toggleCallDetail('tool-1');
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('img').getAttribute('src')).toBe(
        'data:image/png;base64,abc',
      );
    });
  });

  describe('call detail (third disclosure level)', () => {
    it('keeps raw input and result folded when the rail is expanded', () => {
      // Expanding says WHICH steps ran; a wall of JSON under every row buries
      // the summaries that make the rail readable in the first place.
      render(
        makeGroup({
          calls: [
            makeCall({
              result: { status: 'success', content: [{ text: 'CS 101 · 3 sections' }] },
            }),
          ],
        }),
      );
      component.toggleExpanded();
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.call-description')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('.call-input')).toBeNull();
      expect(fixture.nativeElement.querySelector('.call-result')).toBeNull();
    });

    it('reveals the detail on click and folds it again', () => {
      render(makeGroup());
      component.toggleExpanded();
      fixture.detectChanges();

      fixture.nativeElement.querySelector('.call-toggle').click();
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.call-input')).toBeTruthy();

      fixture.nativeElement.querySelector('.call-toggle').click();
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.call-input')).toBeNull();
    });

    it('tracks detail per call', () => {
      render(
        makeGroup({ calls: [makeCall({ id: 'a' }), makeCall({ id: 'b' })] }),
      );

      component.toggleCallDetail('a');
      expect(component.isCallDetailOpen(makeCall({ id: 'a' }))).toBe(true);
      expect(component.isCallDetailOpen(makeCall({ id: 'b' }))).toBe(false);
    });

    it('always shows a call that is still streaming its output', () => {
      // The live "generating" preview is the point of the streaming path;
      // folding it away would make a generating artifact look like nothing
      // was happening.
      const streaming = makeCall({
        id: 'gen',
        status: 'pending',
        streamingContent: '<html>partial',
        result: undefined,
      });
      expect(component.isCallDetailOpen(streaming)).toBe(true);
    });

    it('exposes the detail state to assistive technology', () => {
      render(makeGroup());
      component.toggleExpanded();
      fixture.detectChanges();

      const toggle = fixture.nativeElement.querySelector('.call-toggle');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');

      toggle.click();
      fixture.detectChanges();
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
    });
  });

  describe('timing and failures', () => {
    it('sums measured durations across the group', () => {
      render(
        makeGroup({
          calls: [
            makeCall({ id: 'a', durationMs: 250 }),
            makeCall({ id: 'b', durationMs: 1250 }),
          ],
        }),
      );

      expect(component.totalDurationMs()).toBe(1500);
      expect(
        fixture.nativeElement.querySelector('.total-duration').textContent,
      ).toContain('1.5s');
    });

    it('shows no total when nothing was timed', () => {
      // Durations are live-only; a reloaded conversation has none, and "0ms"
      // would be a number the user could not trust.
      render(makeGroup());

      expect(component.totalDurationMs()).toBeNull();
      expect(fixture.nativeElement.querySelector('.total-duration')).toBeNull();
    });

    it('counts failures, including a failure inside a successful invocation', () => {
      render(
        makeGroup({
          calls: [
            makeCall({ id: 'a', status: 'complete' }),
            makeCall({ id: 'b', status: 'error' }),
            makeCall({
              id: 'c',
              status: 'complete',
              result: { status: 'error', content: [{ text: '422' }] },
            }),
          ],
        }),
      );

      expect(component.failureCount()).toBe(2);
      expect(
        fixture.nativeElement.querySelector('.failure-count').textContent,
      ).toContain('2 failed');
    });

    it('shows no failure chip for a clean group', () => {
      render(makeGroup());
      expect(component.failureCount()).toBeNull();
      expect(fixture.nativeElement.querySelector('.failure-count')).toBeNull();
    });
  });

  describe('status dots', () => {
    it('marks a complete call green', () => {
      expect(component.statusDotClass(makeCall({ status: 'complete' }))).toContain(
        'bg-state-success-500',
      );
    });

    it('marks a pending call with the shimmer', () => {
      const cls = component.statusDotClass(makeCall({ status: 'pending' }));
      expect(cls).toContain('bg-state-warning-400');
      expect(cls).toContain('shimmer');
    });

    it('marks an error call red', () => {
      expect(component.statusDotClass(makeCall({ status: 'error' }))).toContain(
        'bg-state-danger-500',
      );
    });

    it('marks a consent-gated call distinctly from an error', () => {
      // "Paused for authorization" is not a failure, and rendering it as one
      // sends the user looking for a bug instead of a Connect button.
      const cls = component.statusDotClass(makeCall({ status: 'awaiting_auth' }));
      expect(cls).toContain('bg-primary-500');
      expect(cls).not.toContain('danger');
    });
  });

  describe('helper methods', () => {
    it('formats duration in seconds at or above 1000ms', () => {
      expect(component.formatDuration(1500)).toBe('1.5s');
    });

    it('formats duration in milliseconds below 1000ms', () => {
      expect(component.formatDuration(250)).toBe('250ms');
    });

    it('formats input as key-value pairs', () => {
      const result = component.formatInput({ query: 'test', limit: 5 });
      expect(result).toContain('query: "test"');
      expect(result).toContain('limit: 5');
    });

    it('truncates long text', () => {
      const truncated = component.truncateResult('A'.repeat(300), 200);
      expect(truncated.length).toBe(203);
      expect(truncated.endsWith('...')).toBe(true);
    });

    it('leaves short text alone', () => {
      expect(component.truncateResult('Hello')).toBe('Hello');
    });

    it('builds an image data URL', () => {
      expect(
        component.getImageDataUrl({ image: { format: 'jpeg', data: 'abc123' } }),
      ).toBe('data:image/jpeg;base64,abc123');
    });

    it('returns an empty string for non-image content', () => {
      expect(component.getImageDataUrl({ text: 'hello' })).toBe('');
    });

    it('combines text and json result items', () => {
      const text = component.getResultText(
        makeCall({
          result: {
            status: 'success',
            content: [{ text: 'hello' }, { json: { key: 'value' } }],
          },
        }),
      );
      expect(text).toContain('hello');
      expect(text).toContain('"key"');
    });

    it('represents an image item as [image]', () => {
      expect(
        component.getResultText(
          makeCall({
            result: { status: 'success', content: [{ image: { format: 'png', data: 'x' } }] },
          }),
        ),
      ).toBe('[image]');
    });
  });
});
