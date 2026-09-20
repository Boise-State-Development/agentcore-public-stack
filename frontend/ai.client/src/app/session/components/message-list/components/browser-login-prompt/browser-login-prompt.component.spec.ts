import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';

import { BrowserLoginPromptComponent } from './browser-login-prompt.component';
import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../../../services/browser-login/browser-login.service';

const SANDBOX = 'https://mcp-sandbox.example.test';
const SIGNED =
  'https://bedrock-agentcore.us-west-2.amazonaws.com/live?X-Amz-Signature=deadbeef';

function request(overrides: Partial<BrowserLoginRequest> = {}): BrowserLoginRequest {
  return {
    interruptId: 'v1:tool_call:tu-1:abc',
    toolUseId: 'tu-1',
    sessionId: 'conv-1',
    browserSessionId: 'bs-1',
    browserId: 'browser-abc',
    viewport: { width: 1280, height: 800 },
    sandboxOrigin: SANDBOX,
    reason: 'Sign in to JSTOR so I can check the results page.',
    targetUrl: 'https://www.jstor.org/action/showLogin',
    deadlineAt: new Date(Date.now() + 480_000).toISOString(),
    receivedAt: Date.now(),
    ...overrides,
  };
}

describe('BrowserLoginPromptComponent', () => {
  let fixture: ComponentFixture<BrowserLoginPromptComponent>;
  let service: {
    mintLiveView: ReturnType<typeof vi.fn>;
    hasLapsed: ReturnType<typeof vi.fn>;
    complete: ReturnType<typeof vi.fn>;
    skip: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    service = {
      mintLiveView: vi.fn().mockResolvedValue({
        url: SIGNED,
        expiresAt: new Date(Date.now() + 300_000).toISOString(),
        viewport: { width: 1280, height: 800 },
        controlState: 'user',
      }),
      hasLapsed: vi.fn().mockReturnValue(false),
      complete: vi.fn().mockResolvedValue(undefined),
      skip: vi.fn().mockResolvedValue(undefined),
    };

    await TestBed.configureTestingModule({
      imports: [BrowserLoginPromptComponent],
      // DI token over vi.mock, per the repo's testing convention.
      providers: [{ provide: BrowserLoginService, useValue: service }],
    }).compileComponents();

    fixture = TestBed.createComponent(BrowserLoginPromptComponent);
  });

  afterEach(() => fixture.destroy());

  function render(req: BrowserLoginRequest = request()) {
    fixture.componentRef.setInput('request', req);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  describe('what the user is told', () => {
    it("shows the agent's reason and the page being signed into", () => {
      const el = render();

      expect(el.textContent).toContain('Sign in to JSTOR');
      expect(el.textContent).toContain('jstor.org/action/showLogin');
    });

    it('discloses that the browser is ours and the agent is locked out', () => {
      // Non-negotiable: the user is about to type a password into a browser
      // running in our AWS account.
      const el = render();

      expect(el.textContent).toContain('running in our cloud');
      expect(el.textContent?.toLowerCase()).toContain('locked out');
    });

    it('offers no viewer when the sandbox origin is not deployed', () => {
      const el = render(request({ sandboxOrigin: '' }));

      expect(el.textContent).toContain("isn't available in this environment");
      expect(el.querySelector('iframe')).toBeNull();
    });

    it('stops offering the viewer once the window has closed', () => {
      service.hasLapsed.mockReturnValue(true);
      const el = render();

      expect(el.textContent).toContain('sign-in window closed');
      expect(el.textContent).toContain('Dismiss');
      expect(el.querySelector('iframe')).toBeNull();
    });
  });

  describe('opening the viewer', () => {
    it('mints before framing, so a failure is a message not a black box', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      expect(service.mintLiveView).toHaveBeenCalledWith('conv-1');
      expect(el.querySelector('iframe')).not.toBeNull();
    });

    it('mints once per open, however many times the button is activated', async () => {
      const el = render();
      const open = el.querySelector('button') as HTMLButtonElement;

      // Two activations in the same tick: measured on dev as two
      // `POST .../browser/live-view` 30ms apart for a single open. Each mint
      // is a live signed credential, and the second drove a second
      // `dcv.authenticate` that failed while the first was still connecting.
      open.click();
      open.click();
      await fixture.whenStable();
      fixture.detectChanges();

      expect(service.mintLiveView).toHaveBeenCalledTimes(1);
    });

    it('retires the open affordance once the viewer is up', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      // The re-entry guard covers a programmatic second call; this covers the
      // UI half — there is no longer a control that could ask for another
      // mint. Asserted as "the button is gone" rather than clicking it again,
      // because clicking a control that does not exist passes whether or not
      // the guard works.
      const stillOffersOpen = Array.from(el.querySelectorAll('button')).some((b) =>
        b.textContent?.includes('Open sign-in'),
      );
      expect(stillOffersOpen).toBe(false);
      expect(service.mintLiveView).toHaveBeenCalledTimes(1);
    });

    it('frames the sandbox origin and declares the stream host in the CSP', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      const src = el.querySelector('iframe')!.getAttribute('src')!;
      expect(src.startsWith(`${SANDBOX}/live-view.html?csp=`)).toBe(true);

      // connect-src must name the minted URL's own origin — without it DCV's
      // WebSocket is blocked and the stream is silently black.
      const csp = JSON.parse(decodeURIComponent(src.split('csp=')[1]));
      expect(csp.connectDomains).toContain(
        'https://bedrock-agentcore.us-west-2.amazonaws.com',
      );
      expect(csp.connectDomains).toContain(
        'wss://bedrock-agentcore.us-west-2.amazonaws.com',
      );
    });

    it('never puts the signed URL in the frame src', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      const src = el.querySelector('iframe')!.getAttribute('src')!;
      expect(src).not.toContain('X-Amz-Signature');
    });

    it('surfaces a mint failure instead of framing an empty viewer', async () => {
      service.mintLiveView.mockRejectedValue(new Error('gone'));
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      expect(el.textContent).toContain('Could not open the sign-in viewer');
      expect(el.querySelector('iframe')).toBeNull();
    });

    it('sizes the frame to the session viewport, not a constant', async () => {
      const el = render(request({ viewport: { width: 1600, height: 900 } }));
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      const frame = el.querySelector('iframe')!.parentElement as HTMLElement;
      expect(frame.style.aspectRatio.replace(/\s/g, '')).toBe('1600/900');
    });
  });

  describe('full screen', () => {
    async function openViewer(req = request()) {
      const el = render(req);
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();
      return el;
    }

    function expand(el: HTMLElement) {
      const btn = Array.from(el.querySelectorAll('button')).find((b) =>
        b.textContent?.includes('Take over full screen'),
      ) as HTMLButtonElement;
      btn.click();
      fixture.detectChanges();
    }

    it('offers a way into full screen once the viewer is up', async () => {
      const el = await openViewer();

      // The inline frame reads as a screenshot; without this the user has no
      // signal that it is live and drivable.
      const labels = Array.from(el.querySelectorAll('button')).map((b) =>
        b.textContent?.trim(),
      );
      expect(labels.some((t) => t?.includes('Take over full screen'))).toBe(true);
    });

    it('keeps the SAME iframe element when expanding', async () => {
      const el = await openViewer();
      const before = el.querySelector('iframe');

      expand(el);

      // Non-negotiable: re-creating the iframe reloads it, which tears down
      // the DCV stream and loses a half-typed password. Expanding must only
      // restyle the wrapper.
      expect(el.querySelector('iframe')).toBe(before);
    });

    it('says plainly that the user is driving, once expanded', async () => {
      const el = await openViewer();
      expand(el);

      expect(el.textContent).toContain("You're driving this browser");
    });

    it('marks the expanded layer as a modal for assistive tech', async () => {
      const el = await openViewer();
      expand(el);

      const layer = el.querySelector('[role="dialog"]');
      expect(layer).not.toBeNull();
      expect(layer?.getAttribute('aria-modal')).toBe('true');
      expect(layer?.getAttribute('aria-label')).toContain('full screen');
    });

    it('is not a modal while inline, so it never traps the conversation', async () => {
      const el = await openViewer();

      expect(el.querySelector('[role="dialog"]')).toBeNull();
    });

    it('collapses back, keeping the same iframe', async () => {
      const el = await openViewer();
      expand(el);
      const framed = el.querySelector('iframe');

      const exit = Array.from(el.querySelectorAll('button')).find((b) =>
        b.textContent?.includes('Exit full screen'),
      ) as HTMLButtonElement;
      exit.click();
      fixture.detectChanges();

      expect(el.querySelector('[role="dialog"]')).toBeNull();
      expect(el.querySelector('iframe')).toBe(framed);
    });

    it('drops out of full screen when the window closes', async () => {
      const el = await openViewer();
      expand(el);
      expect(el.querySelector('[role="dialog"]')).not.toBeNull();

      // A deadline passing while expanded would otherwise strand a
      // full-viewport black rectangle over the conversation.
      //
      // Driven through the INPUT rather than the clock: `lapsed` depends on
      // both `request()` and the component's 1s ticker, and that ticker is a
      // real `setInterval` created in the constructor — installing fake timers
      // afterwards never drives it, so a timer-based version of this test
      // passed for the wrong reason.
      service.hasLapsed.mockReturnValue(true);
      fixture.componentRef.setInput('request', request({ toolUseId: 'tu-2' }));
      TestBed.tick();
      fixture.detectChanges();

      expect(el.querySelector('[role="dialog"]')).toBeNull();
    });
  });

  describe('the ready handshake', () => {
    it('ignores a ready message from any other origin', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      const frame = el.querySelector('iframe')!;
      const post = vi.fn();
      Object.defineProperty(frame, 'contentWindow', {
        value: { postMessage: post },
        configurable: true,
      });
      post.mockClear();

      // A hostile sibling frame must not be able to provoke us into posting a
      // live signed URL to it.
      window.dispatchEvent(
        new MessageEvent('message', {
          origin: 'https://evil.example',
          data: { type: 'browser-live-view/ready' },
        }),
      );

      expect(post).not.toHaveBeenCalled();
    });

    it('posts the URL to the sandbox origin when the viewer says it is ready', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      const frame = el.querySelector('iframe')!;
      const post = vi.fn();
      Object.defineProperty(frame, 'contentWindow', {
        value: { postMessage: post },
        configurable: true,
      });

      window.dispatchEvent(
        new MessageEvent('message', {
          origin: SANDBOX,
          data: { type: 'browser-live-view/ready' },
        }),
      );

      expect(post).toHaveBeenCalledOnce();
      const [message, target] = post.mock.calls[0];
      expect(message.url).toBe(SIGNED);
      expect(message.viewport).toEqual({ width: 1280, height: 800 });
      // Explicit target, never '*': that origin also serves untrusted MCP App
      // HTML, and a wildcard would post a live credential to whatever is there.
      expect(target).toBe(SANDBOX);
    });
  });

  describe('resolving', () => {
    it('reports completion for this interrupt', async () => {
      const el = render();
      (el.querySelector('button') as HTMLButtonElement).click();
      await fixture.whenStable();
      fixture.detectChanges();

      const done = Array.from(el.querySelectorAll('button')).find((b) =>
        b.textContent?.includes("I've signed in"),
      ) as HTMLButtonElement;
      done.click();
      await fixture.whenStable();

      expect(service.complete).toHaveBeenCalledWith('v1:tool_call:tu-1:abc');
    });

    it('skips when the user declines', async () => {
      const el = render();
      const decline = Array.from(el.querySelectorAll('button')).find((b) =>
        b.textContent?.includes("Don't sign in"),
      ) as HTMLButtonElement;
      decline.click();
      await fixture.whenStable();

      expect(service.skip).toHaveBeenCalledWith('v1:tool_call:tu-1:abc');
    });
  });
});
