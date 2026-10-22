import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';

import { MessageListComponent } from './message-list.component';
import type { Message } from '../../services/models/message.model';
import type { SharedConversationArtifact } from '../../services/share/share.service';

/**
 * Anchoring for artifacts inside a shared conversation.
 *
 * The owner's equivalent (`artifactsByMessageIndex`) has no component
 * spec, so this covers the branch it was modelled on as well as the new
 * one: an artifact whose index is outside the loaded page must fall back
 * to the end strip rather than disappear, which is the failure the
 * whole feature exists to fix.
 */
describe('MessageListComponent — shared conversation artifacts', () => {
  let fixture: ComponentFixture<MessageListComponent>;

  /**
   * Fixtures are user-role messages throughout.
   *
   * Artifacts anchor to assistant turns in production, but the grouping
   * under test keys on the message *id* and is role-agnostic (the
   * template renders the artifact section per message, outside the role
   * branch). Assistant messages render markdown, which pulls ngx-markdown
   * into a KaTeX dependency this suite has no reason to carry — and its
   * async render rejects after teardown, which under `isolate: false`
   * leaks into unrelated spec files.
   */
  function message(index: number, role: 'user' | 'assistant' = 'user'): Message {
    return {
      id: `msg-sess1-${index}`,
      role,
      content: [{ type: 'text', text: 'hi' }],
      createdAt: '2026-01-01T00:00:00Z',
    } as unknown as Message;
  }

  function artifact(
    overrides: Partial<SharedConversationArtifact> = {},
  ): SharedConversationArtifact {
    return {
      artifactId: 'art-1',
      version: 1,
      title: 'Deck',
      contentType: 'text/html',
      producedByMessageIndex: 1,
      ...overrides,
    };
  }

  /** Reaching the grouping the template reaches. */
  function api() {
    return fixture.componentInstance as unknown as {
      sharedArtifactsForMessageId: (
        id: string,
      ) => SharedConversationArtifact[];
      orphanSharedArtifacts: () => SharedConversationArtifact[];
    };
  }

  function render(
    messages: Message[],
    shared: SharedConversationArtifact[] | null,
  ): void {
    fixture = TestBed.createComponent(MessageListComponent);
    fixture.componentRef.setInput('messages', messages);
    fixture.componentRef.setInput('embeddedMode', true);
    fixture.componentRef.setInput('sharedArtifacts', shared);
    fixture.componentRef.setInput('sharedArtifactShareId', 'conv-share-1');
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    // jsdom has no ResizeObserver, and UserMessageComponent constructs one
    // unguarded in ngAfterViewInit — so rendering any message list needs
    // this stub. Same convention as user-message.component.spec.ts.
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    TestBed.configureTestingModule({
      imports: [MessageListComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        // Assistant messages render markdown; the real component tree is
        // used deliberately here so the template's branching is under
        // test, not a mock of it.
        provideMarkdown(),
      ],
    });
  });

  afterEach(() => {
    fixture?.destroy();
    vi.restoreAllMocks();
  });

  it('anchors an artifact under the turn that produced it', () => {
    render(
      [message(0), message(1)],
      [artifact({ producedByMessageIndex: 1 })],
    );

    expect(
      api().sharedArtifactsForMessageId('msg-sess1-1'),
    ).toHaveLength(1);
    expect(api().sharedArtifactsForMessageId('msg-sess1-0')).toEqual([]);
    expect(api().orphanSharedArtifacts()).toEqual([]);
  });

  it('falls back to the end strip when the anchor is missing', () => {
    // Written before the linkage existed. It must still be visible —
    // an artifact that silently disappears is the bug this feature
    // exists to fix.
    render(
      [message(0), message(1)],
      [artifact({ producedByMessageIndex: null })],
    );

    expect(api().orphanSharedArtifacts()).toHaveLength(1);
  });

  it('falls back to the end strip when the turn is not loaded', () => {
    // The index points outside the loaded (paginated) message list.
    render([message(0)], [artifact({ producedByMessageIndex: 42 })]);

    expect(api().sharedArtifactsForMessageId('msg-sess1-0')).toEqual([]);
    expect(api().orphanSharedArtifacts()).toHaveLength(1);
  });

  it('groups several artifacts from one turn', () => {
    render(
      [message(0), message(1)],
      [
        artifact({ artifactId: 'a', producedByMessageIndex: 1 }),
        artifact({ artifactId: 'b', producedByMessageIndex: 1 }),
      ],
    );

    expect(api().sharedArtifactsForMessageId('msg-sess1-1')).toHaveLength(2);
  });

  it('renders nothing in a normal session view', () => {
    // Null is the ordinary case: the session view reads the owner's
    // ArtifactStateService instead, and must not gain a second source.
    render([message(0), message(1)], null);

    expect(api().sharedArtifactsForMessageId('msg-sess1-1')).toEqual([]);
    expect(api().orphanSharedArtifacts()).toEqual([]);
    expect(
      (fixture.nativeElement as HTMLElement).querySelector(
        'app-shared-artifact-card',
      ),
    ).toBeNull();
  });

  it('renders a recipient card, never the owner card', () => {
    render(
      [message(0), message(1)],
      [artifact({ producedByMessageIndex: 1 })],
    );

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('app-shared-artifact-card')).not.toBeNull();
    // The owner card opens the docked panel and carries owner-only
    // actions; it must never render for a recipient.
    expect(el.querySelector('app-artifact-card')).toBeNull();
  });
});
