import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { McpAppFrameComponent } from './mcp-app-frame.component';
import { McpAppStateService } from '../../../../../services/mcp-apps/mcp-app-state.service';
import {
  McpAppCard,
  McpAppCardStateService,
} from '../../../../../services/mcp-apps/mcp-app-card-state.service';
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
 * GitHub's issue_write App re-runs five `ui_get` lookups every time its frame
 * mounts, and the frame re-mounts on every reload. The card store kept one
 * card per run, so the frame's actions chip grew each time the conversation
 * was reopened, and its failure count doubled (dev, 2026-10-18). app-api now
 * keeps one card per distinct call, with a stable `cardId`; reopening a
 * conversation (reset, then hydrate again) must leave the chip as it was.
 */

/** A stand-in whose every member is a no-op returning `undefined`. */
function stub<T extends object>(members: Partial<Record<string, unknown>> = {}): T {
  return new Proxy(members, {
    get: (target, prop: string) => (prop in target ? target[prop] : () => undefined),
  }) as T;
}

const FRAME = 'tooluse_issue_write';

function card(cardId: string, over: Partial<McpAppCard> = {}): McpAppCard {
  return {
    cardId,
    toolUseId: FRAME,
    toolName: 'ui_get',
    arguments: {},
    content: [{ type: 'text', text: '[]' }],
    isError: false,
    createdAt: '2026-10-18T00:00:00Z',
    producedByMessageIndex: null,
    ...over,
  };
}

/** What `GET /mcp-apps/cards` returns for that conversation, on every load. */
const SERVER_CARDS: McpAppCard[] = [
  ...['assignees', 'labels', 'milestones', 'issue_fields', 'issue_types'].map(
    (method, i) =>
      card(`lookup-${method}`, {
        arguments: { method, owner: 'o', repo: 'r' },
        isError: method === 'issue_types',
        createdAt: `2026-10-18T00:00:0${i}Z`,
      }),
  ),
  card('write', {
    toolName: 'issue_write',
    arguments: { method: 'create', title: 'Bug', _ui_submitted: true },
    createdAt: '2026-10-18T00:01:00Z',
  }),
];

describe('McpAppFrameComponent: the actions chip across reopens', () => {
  let fixture: ComponentFixture<McpAppFrameComponent>;
  let cards: McpAppCardStateService;

  beforeEach(async () => {
    cards = new McpAppCardStateService();
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [McpAppFrameComponent],
      providers: [
        { provide: McpAppStateService, useValue: stub() },
        { provide: McpAppCardStateService, useValue: cards },
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
      // The real chip sits inside `@if (canRenderApp())`, which needs a
      // deployed sandbox origin and mounts an iframe. Mirror just the chip,
      // bound to the component's own signals.
      .overrideComponent(McpAppFrameComponent, {
        set: {
          styles: [],
          template: `
            @if (appActions().length) {
              <button class="chip">{{ appActionsChipText() }}</button>
            }
          `,
        },
      })
      .compileComponents();

    fixture = TestBed.createComponent(McpAppFrameComponent);
    fixture.componentRef.setInput('toolUseId', FRAME);
    fixture.componentRef.setInput('result', { status: 'success', content: [] });
    fixture.componentRef.setInput('inputComplete', true);
  });

  function chip(): string | undefined {
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement)
      .querySelector('.chip')
      ?.textContent?.trim();
  }

  /** What the session page does when a conversation is opened again. */
  function reopen(): void {
    cards.reset();
    cards.seedFromHydration(SERVER_CARDS.map((c) => ({ ...c })));
  }

  it('counts each distinct action once, the 404 lookup as one failure', () => {
    cards.seedFromHydration(SERVER_CARDS);
    expect(chip()).toBe('6 actions · 1 failed');
  });

  it('shows the same count after the conversation is reopened, twice', () => {
    cards.seedFromHydration(SERVER_CARDS);
    const first = chip();

    reopen();
    expect(chip()).toBe(first);
    reopen();
    expect(chip()).toBe(first);
  });

  it('does not grow when a second hydration of the same load lands late', () => {
    cards.seedFromHydration(SERVER_CARDS);
    cards.seedFromHydration(SERVER_CARDS.map((c) => ({ ...c })));
    expect(chip()).toBe('6 actions · 1 failed');
  });

  it('shows no chip for a frame with no recorded actions', () => {
    fixture.componentRef.setInput('toolUseId', 'tooluse_other');
    cards.seedFromHydration(SERVER_CARDS);
    expect(chip()).toBeUndefined();
  });
});
