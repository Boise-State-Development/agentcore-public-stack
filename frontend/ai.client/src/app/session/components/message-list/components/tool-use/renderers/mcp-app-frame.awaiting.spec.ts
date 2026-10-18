import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Signal } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';
import { McpAppFrameComponent } from './mcp-app-frame.component';
import { ToolResultData } from '../tool-renderer-registry.service';
import { McpAppStateService } from '../../../../../services/mcp-apps/mcp-app-state.service';
import { McpAppCardStateService } from '../../../../../services/mcp-apps/mcp-app-card-state.service';
import { McpAppProxyService } from '../../../../../services/mcp-apps/mcp-app-proxy.service';
import { McpAppMessageService } from '../../../../../services/mcp-apps/mcp-app-message.service';
import { McpAppConsentService } from '../../../../../services/mcp-apps/mcp-app-consent.service';
import { McpAppTeardownService } from '../../../../../services/mcp-apps/mcp-app-teardown.service';
import { ChatRequestService } from '../../../../../services/chat/chat-request.service';
import { ChatStateService } from '../../../../../services/chat/chat-state.service';
import { SessionService } from '../../../../../services/session/session.service';
import { StreamParserService } from '../../../../../services/chat/stream-parser.service';
import { ThemeService } from '../../../../../../components/topnav/components/theme-toggle/theme.service';

/**
 * GitHub's issue_write App answers the model's call with `isError: true` on
 * purpose while its form waits for Submit (dev, 2026-10-18). The frame showed
 * a red "Failed" badge and "(Error)" on a form that was simply open. The
 * backend now marks that result `structuredContent.status =
 * "awaiting_user_submission"`; the frame must read it as waiting, not failed.
 */

/** A stand-in whose every member is a no-op returning `undefined`. */
function stub<T extends object>(members: Partial<Record<string, unknown>> = {}): T {
  return new Proxy(members, {
    get: (target, prop: string) => (prop in target ? target[prop] : () => undefined),
  }) as T;
}

/** The frame's derived state, which the template reads. */
interface FrameState {
  isAwaitingUser: Signal<boolean>;
  isError: Signal<boolean>;
  isRunning: Signal<boolean>;
}

const AWAITING: ToolResultData = {
  status: 'error',
  content: [{ text: 'An interactive form has been shown to the user…' }],
  structuredContent: { status: 'awaiting_user_submission' },
};

const FAILED: ToolResultData = {
  status: 'error',
  content: [{ text: 'Connection to the MCP server was closed' }],
};

describe('McpAppFrameComponent: an App form waiting for the user', () => {
  let fixture: ComponentFixture<McpAppFrameComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [McpAppFrameComponent],
      providers: [
        { provide: McpAppStateService, useValue: stub() },
        { provide: McpAppCardStateService, useValue: stub({ cardsFor: () => [] }) },
        { provide: McpAppProxyService, useValue: stub() },
        { provide: McpAppMessageService, useValue: stub() },
        { provide: McpAppConsentService, useValue: stub() },
        { provide: McpAppTeardownService, useValue: stub() },
        { provide: ChatRequestService, useValue: stub() },
        { provide: ChatStateService, useValue: stub() },
        { provide: SessionService, useValue: stub() },
        { provide: StreamParserService, useValue: stub() },
        { provide: ThemeService, useValue: stub() },
      ],
    })
      // The real header sits inside `@if (canRenderApp())`, which needs a
      // deployed sandbox origin and mounts an iframe. Mirror just the status
      // badges, bound to the component's own signals.
      .overrideComponent(McpAppFrameComponent, {
        set: {
          styles: [],
          template: `
            <span class="tool-name" [class.tool-name-shimmer]="isRunning()">issue_write</span>
            @if (isAwaitingUser()) {
              <span class="badge-waiting">Waiting for you</span>
            } @else if (isError()) {
              <span class="badge-failed">Failed</span>
            }
          `,
        },
      })
      .compileComponents();

    fixture = TestBed.createComponent(McpAppFrameComponent);
    fixture.componentRef.setInput('toolUseId', 'tooluse_1');
  });

  function render(result: ToolResultData, inputComplete = true): FrameState {
    fixture.componentRef.setInput('result', result);
    fixture.componentRef.setInput('inputComplete', inputComplete);
    fixture.detectChanges();
    return fixture.componentInstance as unknown as FrameState;
  }

  const query = (selector: string) =>
    (fixture.nativeElement as HTMLElement).querySelector(selector);

  it('shows "Waiting for you", not "Failed", for the awaiting marker', () => {
    const state = render(AWAITING);

    expect(state.isAwaitingUser()).toBe(true);
    expect(state.isError()).toBe(false);
    expect(query('.badge-waiting')?.textContent).toBe('Waiting for you');
    expect(query('.badge-failed')).toBeNull();
  });

  it('does not shimmer while waiting on the user', () => {
    const state = render(AWAITING, false);

    expect(state.isRunning()).toBe(false);
    expect(query('.tool-name')?.classList.contains('tool-name-shimmer')).toBe(false);
  });

  it('still shows "Failed" for a real failure', () => {
    const state = render(FAILED);

    expect(state.isAwaitingUser()).toBe(false);
    expect(state.isError()).toBe(true);
    expect(query('.badge-failed')?.textContent).toBe('Failed');
    expect(query('.badge-waiting')).toBeNull();
  });

  it('shows neither badge for a success', () => {
    render({ status: 'success', content: [{ text: '{"id": 1}' }] });

    expect(query('.badge-failed')).toBeNull();
    expect(query('.badge-waiting')).toBeNull();
  });
});
