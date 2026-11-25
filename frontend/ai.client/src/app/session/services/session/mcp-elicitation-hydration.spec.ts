import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageMapService } from './message-map.service';
import { SessionService } from './session.service';
import { FileUploadService } from '../../../services/file-upload';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { McpElicitationService } from '../../../services/mcp-elicitation/mcp-elicitation.service';

/**
 * Replaying a paused MCP server form after a page refresh
 * (docs/specs/mcp-elicitation.md §5, "Reload").
 *
 * The event fires once and never re-streams, so the `mcp_elicitation`
 * breadcrumb is the only way back to the form. Before this, the hydrator fell
 * through to the OAuth branch for this kind and dropped it for having no
 * `providerId`, leaving the turn paused with nothing to answer.
 */
const ID = 'v1:middleware_execute_tool:tu-1:abc';

const PAYLOAD = {
  type: 'mcp_elicitation_required',
  interruptId: ID,
  toolUseId: 'tu-1',
  toolName: 'github_issues___delete_repository',
  serverName: 'GitHub',
  message: 'Type "acme/scratch" to confirm permanent deletion.',
  fields: [{ name: 'repository_name', kind: 'text', title: 'Repository name', required: true }],
};

function interrupt(overrides: Record<string, unknown> = {}) {
  return {
    interruptId: ID,
    kind: 'mcp_elicitation',
    toolUseId: 'tu-1',
    elicitation: JSON.stringify(PAYLOAD),
    createdAt: '2026-11-25T00:00:00Z',
    ...overrides,
  };
}

describe('MessageMapService — mcp_elicitation hydration', () => {
  let service: MessageMapService;
  let sessionService: { getMessages: ReturnType<typeof vi.fn> };
  let elicitation: McpElicitationService;
  let consent: { requestConsent: ReturnType<typeof vi.fn> };

  async function hydrate(pendingInterrupts: unknown[], messages: unknown[] = []) {
    sessionService.getMessages.mockResolvedValue({ messages, pendingInterrupts });
    await service.loadMessagesForSession('s1');
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    sessionService = {
      getMessages: vi.fn().mockResolvedValue({ messages: [] }),
      isNewSession: vi.fn().mockReturnValue(false),
      updateSessionTitleInCache: vi.fn(),
    } as never;
    consent = { requestConsent: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        MessageMapService,
        McpElicitationService,
        { provide: SessionService, useValue: sessionService },
        { provide: FileUploadService, useValue: { listSessionFiles: vi.fn().mockResolvedValue([]) } },
        { provide: OAuthConsentService, useValue: consent },
        { provide: ToolApprovalService, useValue: { requestApproval: vi.fn() } },
      ],
    });
    service = TestBed.inject(MessageMapService);
    elicitation = TestBed.inject(McpElicitationService);
  });

  it('re-renders the form from the breadcrumb', async () => {
    await hydrate([interrupt({ triggeringMessageId: 'm-3' })]);

    const [pending] = elicitation.pending();
    expect(pending).toMatchObject({
      interruptId: ID,
      toolUseId: 'tu-1',
      toolName: 'github_issues___delete_repository',
      serverName: 'GitHub',
      message: PAYLOAD.message,
      fields: PAYLOAD.fields,
      messageId: 'm-3',
      sessionId: 's1',
    });
    // Never mistaken for an OAuth row.
    expect(consent.requestConsent).not.toHaveBeenCalled();
  });

  it.each([
    ['unparseable', 'not json'],
    ['missing', undefined],
    ['a form with no fields', JSON.stringify({ ...PAYLOAD, fields: [] })],
    ['another event', JSON.stringify({ ...PAYLOAD, type: 'user_question_required' })],
  ])('drops a breadcrumb that is %s', async (_label, value) => {
    await hydrate([interrupt({ elicitation: value })]);

    expect(elicitation.hasPending()).toBe(false);
    expect(consent.requestConsent).not.toHaveBeenCalled();
  });

  it('does not bring back a form the user already answered', async () => {
    await hydrate([interrupt()]);
    await elicitation.decline(ID);

    await hydrate([interrupt()]);

    expect(elicitation.hasPending()).toBe(false);
  });
});
