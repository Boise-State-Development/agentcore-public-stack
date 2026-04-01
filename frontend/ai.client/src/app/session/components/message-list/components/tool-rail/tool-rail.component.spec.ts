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
      .overrideComponent(ToolRailComponent, {
        set: {
          templateUrl: undefined as any,
          styleUrl: undefined as any,
          styles: [],
          template: `
            <button type="button" (click)="toggleExpanded()" class="flex items-center gap-2 cursor-pointer group w-full text-left">
              @if (hasSummaries() && group().groupSummary) {
                <span>{{ group().groupSummary }}</span>
              } @else {
                <span class="flex items-center gap-1.5 flex-wrap">
                  @if (effectiveExpanded()) {
                    @for (call of group().calls; track call.id; let last = $last) {
                      <span>{{ call.toolName }}</span>
                      @if (!last) { <span>&rarr;</span> }
                    }
                    <span>({{ group().calls.length }} tools)</span>
                  } @else {
                    @for (call of collapsedHeaderCalls(); track call.id; let last = $last) {
                      <span>{{ call.toolName }}</span>
                      @if (!last) { <span>&rarr;</span> }
                    }
                    @if (overflowCount() > 0) {
                      <span>and {{ overflowCount() }} more</span>
                    }
                  }
                </span>
              }
            </button>
            <div class="collapsible-content" [class.open]="effectiveExpanded()">
              <div><div class="pl-4 space-y-3 mt-2 border-l-2">
                @for (call of group().calls; track call.id) {
                  <div>
                    <div class="flex items-center gap-2 mb-0.5">
                      <span [class]="statusDotClass(call)"></span>
                      <span class="text-xs font-medium">{{ call.toolName }}</span>
                      @if (call.durationMs) { <span>{{ formatDuration(call.durationMs) }}</span> }
                    </div>
                    @if (hasSummaries() && call.summary) {
                      <p class="ml-3.5">{{ call.summary }}</p>
                    }
                    @if (!hasSummaries()) {
                      <div class="ml-3.5 space-y-1">
                        @if (call.input && (call.input | keyvalue)?.length) {
                          <div><span>input:</span><span class="font-mono">{{ formatInput(call.input) }}</span></div>
                        }
                        @if (call.result) {
                          <div class="flex items-start gap-1.5">
                            <span>result:</span>
                            <div class="result-block">
                              @if (isResultExpanded(call.id)) {
                                @for (item of call.result.content; track $index) {
                                  @if (item.json) { <pre><code [innerHTML]="formatResultContent(item) | jsonSyntaxHighlight"></code></pre> }
                                  @else if (item.text) { <div>{{ item.text }}</div> }
                                }
                              } @else {
                                <span>{{ truncateResult(getResultText(call)) }}</span>
                              }
                              @if (getResultText(call).length > 200) {
                                <button type="button" (click)="toggleFullResult(call.id)">
                                  {{ isResultExpanded(call.id) ? 'Show less' : 'Show full result' }}
                                </button>
                              }
                            </div>
                          </div>
                          @for (item of getResultImages(call); track $index) {
                            <div><img [src]="getImageDataUrl(item)" alt="Tool result image" /></div>
                          }
                        }
                      </div>
                    }
                  </div>
                }
              </div></div>
            </div>
          `,
      },
    })
    .compileComponents();

    fixture = TestBed.createComponent(ToolRailComponent);
    component = fixture.componentInstance;
  });

  describe('single tool call', () => {
    it('should render a rail with one item', () => {
      fixture.componentRef.setInput('group', makeGroup());
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector('button');
      expect(button).toBeTruthy();
      expect(button.textContent).toContain('search_classes');
    });
  });

  describe('multiple consecutive tool calls', () => {
    it('should show up to 3 tool names in collapsed header', () => {
      const group = makeGroup({
        calls: [
          makeCall({ id: 'tool-1', toolName: 'google_drive_search' }),
          makeCall({ id: 'tool-2', toolName: 'web_search' }),
          makeCall({ id: 'tool-3', toolName: 'web_fetch' }),
        ],
      });
      fixture.componentRef.setInput('group', group);
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector('button');
      expect(button.textContent).toContain('google_drive_search');
      expect(button.textContent).toContain('web_search');
      expect(button.textContent).toContain('web_fetch');
      // No overflow text when exactly at the limit
      expect(button.textContent).not.toContain('and');
    });

    it('should show "and X more" when collapsed with >3 tool calls', () => {
      const group = makeGroup({
        calls: [
          makeCall({ id: 'tool-1', toolName: 'tool_a' }),
          makeCall({ id: 'tool-2', toolName: 'tool_b' }),
          makeCall({ id: 'tool-3', toolName: 'tool_c' }),
          makeCall({ id: 'tool-4', toolName: 'tool_d' }),
          makeCall({ id: 'tool-5', toolName: 'tool_e' }),
        ],
      });
      fixture.componentRef.setInput('group', group);
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector('button');
      // Collapsed: first 3 shown, rest summarized
      expect(button.textContent).toContain('tool_a');
      expect(button.textContent).toContain('tool_b');
      expect(button.textContent).toContain('tool_c');
      expect(button.textContent).not.toContain('tool_d');
      expect(button.textContent).not.toContain('tool_e');
      expect(button.textContent).toContain('and 2 more');
    });

    it('should show all tool names with count when expanded', () => {
      const group = makeGroup({
        calls: [
          makeCall({ id: 'tool-1', toolName: 'tool_a' }),
          makeCall({ id: 'tool-2', toolName: 'tool_b' }),
          makeCall({ id: 'tool-3', toolName: 'tool_c' }),
          makeCall({ id: 'tool-4', toolName: 'tool_d' }),
        ],
      });
      fixture.componentRef.setInput('group', group);
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector('button');
      expect(button.textContent).toContain('tool_a');
      expect(button.textContent).toContain('tool_b');
      expect(button.textContent).toContain('tool_c');
      expect(button.textContent).toContain('tool_d');
      expect(button.textContent).toContain('(4 tools)');
      expect(button.textContent).not.toContain('and');
    });

    it('should show arrow separators between tool names', () => {
      const group = makeGroup({
        calls: [
          makeCall({ id: 'tool-1', toolName: 'tool_a' }),
          makeCall({ id: 'tool-2', toolName: 'tool_b' }),
        ],
      });
      fixture.componentRef.setInput('group', group);
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector('button');
      // → is the rendered form of &rarr;
      expect(button.textContent).toContain('→');
    });
  });

  describe('expand/collapse', () => {
    it('should start collapsed when all tools are complete', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({ status: 'complete' })],
      }));
      fixture.detectChanges();

      expect(component.effectiveExpanded()).toBe(false);
    });

    it('should auto-expand when any tool is pending', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [
          makeCall({ id: 'tool-1', status: 'complete' }),
          makeCall({ id: 'tool-2', status: 'pending' }),
        ],
      }));
      fixture.detectChanges();

      expect(component.shouldAutoExpand()).toBe(true);
      expect(component.effectiveExpanded()).toBe(true);
    });

    it('should toggle expanded state on click', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({ status: 'complete' })],
      }));
      fixture.detectChanges();

      expect(component.effectiveExpanded()).toBe(false);

      component.toggleExpanded();
      expect(component.effectiveExpanded()).toBe(true);

      component.toggleExpanded();
      expect(component.effectiveExpanded()).toBe(false);
    });
  });

  describe('status dots', () => {
    it('should return green dot class for complete status', () => {
      const call = makeCall({ status: 'complete' });
      expect(component.statusDotClass(call)).toContain('bg-green-500');
    });

    it('should return amber shimmer dot class for pending status', () => {
      const call = makeCall({ status: 'pending' });
      expect(component.statusDotClass(call)).toContain('bg-amber-400');
      expect(component.statusDotClass(call)).toContain('shimmer');
    });

    it('should return red dot class for error status', () => {
      const call = makeCall({ status: 'error' });
      expect(component.statusDotClass(call)).toContain('bg-red-500');
    });
  });

  describe('fallback mode (no summaries)', () => {
    it('should detect fallback mode when no summaries exist', () => {
      fixture.componentRef.setInput('group', makeGroup());
      fixture.detectChanges();

      expect(component.hasSummaries()).toBe(false);
    });

    it('should not render input row when input is empty', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({ input: {} })],
      }));
      fixture.detectChanges();

      // Expand the rail to see details
      component.toggleExpanded();
      fixture.detectChanges();

      const inputLabel = fixture.nativeElement.querySelector('.ml-3\\.5 span');
      // No "input:" label should be rendered
      const allText = fixture.nativeElement.textContent;
      // The word "input:" should not appear in expanded content
      // (it will be in the collapsed header area but not in the detail section)
      const detailSection = fixture.nativeElement.querySelector('.collapsible-content');
      expect(detailSection.textContent).not.toContain('input:');
    });

    it('should render input for non-empty input objects', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({ input: { query: 'test' } })],
      }));
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const detailSection = fixture.nativeElement.querySelector('.collapsible-content');
      expect(detailSection.textContent).toContain('input:');
      expect(detailSection.textContent).toContain('query:');
    });

    it('should show truncated result text', () => {
      const longText = 'A'.repeat(300);
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({
          result: { status: 'success', content: [{ text: longText }] },
        })],
      }));
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const detailSection = fixture.nativeElement.querySelector('.collapsible-content');
      expect(detailSection.textContent).toContain('...');
      expect(detailSection.textContent).toContain('Show full result');
    });

    it('should not show "Show full result" for short results', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({
          result: { status: 'success', content: [{ text: 'short result' }] },
        })],
      }));
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const detailSection = fixture.nativeElement.querySelector('.collapsible-content');
      expect(detailSection.textContent).not.toContain('Show full result');
    });
  });

  describe('summary mode', () => {
    it('should detect summary mode when groupSummary is set', () => {
      fixture.componentRef.setInput('group', makeGroup({
        groupSummary: 'Searched Drive and fetched 2 docs',
      }));
      fixture.detectChanges();

      expect(component.hasSummaries()).toBe(true);
    });

    it('should detect summary mode when any call has a summary', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({ summary: 'Found 5 CS courses' })],
      }));
      fixture.detectChanges();

      expect(component.hasSummaries()).toBe(true);
    });

    it('should render groupSummary in the header', () => {
      fixture.componentRef.setInput('group', makeGroup({
        groupSummary: 'Searched Drive and fetched 2 docs',
        calls: [makeCall({ summary: 'Found results' })],
      }));
      fixture.detectChanges();

      const button = fixture.nativeElement.querySelector('button');
      expect(button.textContent).toContain('Searched Drive and fetched 2 docs');
    });

    it('should render per-call summaries in expanded view', () => {
      fixture.componentRef.setInput('group', makeGroup({
        groupSummary: 'Group summary',
        calls: [makeCall({ summary: 'Found 5 CS courses' })],
      }));
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const detailSection = fixture.nativeElement.querySelector('.collapsible-content');
      expect(detailSection.textContent).toContain('Found 5 CS courses');
    });
  });

  describe('result expand/collapse', () => {
    it('should toggle individual result expansion', () => {
      expect(component.isResultExpanded('tool-1')).toBe(false);

      component.toggleFullResult('tool-1');
      expect(component.isResultExpanded('tool-1')).toBe(true);

      component.toggleFullResult('tool-1');
      expect(component.isResultExpanded('tool-1')).toBe(false);
    });

    it('should track multiple expanded results independently', () => {
      component.toggleFullResult('tool-1');
      component.toggleFullResult('tool-2');

      expect(component.isResultExpanded('tool-1')).toBe(true);
      expect(component.isResultExpanded('tool-2')).toBe(true);

      component.toggleFullResult('tool-1');
      expect(component.isResultExpanded('tool-1')).toBe(false);
      expect(component.isResultExpanded('tool-2')).toBe(true);
    });
  });

  describe('image results', () => {
    it('should render images from result content', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({
          result: {
            status: 'success',
            content: [{
              image: { format: 'png', data: 'iVBORw0KGgo=' },
            }],
          },
        })],
      }));
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const img = fixture.nativeElement.querySelector('img');
      expect(img).toBeTruthy();
      expect(img.src).toContain('data:image/png;base64,iVBORw0KGgo=');
    });
  });

  describe('error status', () => {
    it('should show red status dot for error tool calls', () => {
      fixture.componentRef.setInput('group', makeGroup({
        calls: [makeCall({ status: 'error' })],
      }));
      fixture.detectChanges();

      component.toggleExpanded();
      fixture.detectChanges();

      const dot = fixture.nativeElement.querySelector('.bg-red-500');
      expect(dot).toBeTruthy();
    });
  });

  describe('helper methods', () => {
    it('should format duration in seconds for >= 1000ms', () => {
      expect(component.formatDuration(1500)).toBe('1.5s');
    });

    it('should format duration in milliseconds for < 1000ms', () => {
      expect(component.formatDuration(250)).toBe('250ms');
    });

    it('should format input as key-value pairs', () => {
      const result = component.formatInput({ query: 'test', limit: 5 });
      expect(result).toContain('query: "test"');
      expect(result).toContain('limit: 5');
    });

    it('should truncate long text', () => {
      const long = 'A'.repeat(300);
      const truncated = component.truncateResult(long, 200);
      expect(truncated.length).toBe(203); // 200 + '...'
      expect(truncated.endsWith('...')).toBe(true);
    });

    it('should not truncate short text', () => {
      const short = 'Hello';
      expect(component.truncateResult(short)).toBe('Hello');
    });

    it('should build image data URL', () => {
      const url = component.getImageDataUrl({
        image: { format: 'jpeg', data: 'abc123' },
      });
      expect(url).toBe('data:image/jpeg;base64,abc123');
    });

    it('should return empty string for non-image content', () => {
      expect(component.getImageDataUrl({ text: 'hello' })).toBe('');
    });

    it('should get result text combining text and json items', () => {
      const call = makeCall({
        result: {
          status: 'success',
          content: [
            { text: 'hello' },
            { json: { key: 'value' } },
          ],
        },
      });
      const text = component.getResultText(call);
      expect(text).toContain('hello');
      expect(text).toContain('"key"');
    });

    it('should return [image] for image items in getResultText', () => {
      const call = makeCall({
        result: {
          status: 'success',
          content: [{ image: { format: 'png', data: 'x' } }],
        },
      });
      expect(component.getResultText(call)).toBe('[image]');
    });
  });
});
