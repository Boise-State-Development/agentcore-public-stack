import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';

import { BrowserPanelComponent } from './browser-panel.component';
import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../../../services/browser-login/browser-login.service';
import { BrowserPaneStateService } from '../../../../services/browser-pane/browser-pane-state.service';
import { DockedPaneService } from '../../../../services/docked-pane/docked-pane.service';

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

describe('BrowserPanelComponent', () => {
  let fixture: ComponentFixture<BrowserPanelComponent>;
  let pane: BrowserPaneStateService;
  let docked: DockedPaneService;
  let service: {
    pending: ReturnType<typeof signal<BrowserLoginRequest[]>>;
    mintLiveView: ReturnType<typeof vi.fn>;
    hasLapsed: ReturnType<typeof vi.fn>;
    complete: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    service = {
      pending: signal<BrowserLoginRequest[]>([]),
      mintLiveView: vi.fn().mockResolvedValue({
        url: SIGNED,
        expiresAt: new Date(Date.now() + 300_000).toISOString(),
        viewport: { width: 1280, height: 800 },
        controlState: 'user',
      }),
      hasLapsed: vi.fn().mockReturnValue(false),
      complete: vi.fn().mockResolvedValue(undefined),
    };

    await TestBed.configureTestingModule({
      imports: [BrowserPanelComponent],
      // DI token over vi.mock, per the repo's testing convention.
      providers: [{ provide: BrowserLoginService, useValue: service }],
    }).compileComponents();

    pane = TestBed.inject(BrowserPaneStateService);
    docked = TestBed.inject(DockedPaneService);
    fixture = TestBed.createComponent(BrowserPanelComponent);
  });

  afterEach(() => fixture.destroy());

  async function openPanel(req: BrowserLoginRequest = request()) {
    service.pending.set([req]);
    pane.open(req);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('renders nothing while closed', () => {
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('aside')).toBeNull();
    expect(service.mintLiveView).not.toHaveBeenCalled();
  });

  describe('what the user is told', () => {
    it('names the site being signed into', async () => {
      const el = await openPanel();
      expect(el.querySelector('h2')?.textContent).toContain('Sign in to jstor.org');
    });

    it('discloses that the browser is ours and the agent is locked out', async () => {
      // Non-negotiable: the user is about to type a password into a browser
      // running in our AWS account.
      const el = await openPanel();

      expect(el.textContent).toContain('runs in our cloud');
      expect(el.textContent?.toLowerCase()).toContain('locked out');
    });

    it('is a docked pane, not a modal, so it never traps the conversation', async () => {
      const el = await openPanel();

      expect(el.querySelector('aside')).not.toBeNull();
      expect(el.querySelector('[role="dialog"]')).toBeNull();
      expect(docked.owner()).toBe('browser');
    });
  });

  describe('opening the viewer', () => {
    it('mints before framing, so a failure is a message not a black box', async () => {
      const el = await openPanel();

      expect(service.mintLiveView).toHaveBeenCalledWith('conv-1');
      expect(el.querySelector('iframe')).not.toBeNull();
    });

    it('mints once per open, however often the pane re-renders', async () => {
      // Each mint is a live signed credential, and a second one drove a second
      // `dcv.authenticate` that failed while the first was still connecting.
      const req = request();
      await openPanel(req);
      service.pending.set([{ ...req }]);
      fixture.detectChanges();
      await fixture.whenStable();

      expect(service.mintLiveView).toHaveBeenCalledTimes(1);
    });

    it('frames the sandbox origin and declares the stream host in the CSP', async () => {
      const el = await openPanel();

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
      const el = await openPanel();

      const src = el.querySelector('iframe')!.getAttribute('src')!;
      expect(src).not.toContain('X-Amz-Signature');
    });

    it('surfaces a mint failure instead of framing an empty viewer', async () => {
      service.mintLiveView.mockRejectedValue(new Error('gone'));
      const el = await openPanel();

      expect(el.textContent).toContain('Could not open the sign-in viewer');
      expect(el.querySelector('iframe')).toBeNull();
    });

    it('sizes the frame to the session viewport, not a constant', async () => {
      const el = await openPanel(request({ viewport: { width: 1600, height: 900 } }));

      const frame = el.querySelector('iframe')!.parentElement as HTMLElement;
      expect(frame.style.aspectRatio.replace(/\s/g, '')).toBe('1600/900');
    });
  });

  describe('drawing the stream at 1:1', () => {
    let innerWidth: PropertyDescriptor | undefined;

    beforeEach(() => {
      innerWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth');
    });

    afterEach(() => {
      if (innerWidth) Object.defineProperty(window, 'innerWidth', innerWidth);
    });

    function windowWidth(px: number) {
      Object.defineProperty(window, 'innerWidth', { value: px, configurable: true });
    }

    const signIn = () => request({ viewport: { width: 768, height: 832 } });

    function frameWidth(): string {
      return (fixture.componentInstance['frameWidth'] as () => string)();
    }

    it('never draws the frame wider than the remote viewport', async () => {
      await openPanel(signIn());

      // A wider rail centres the frame; it does not upscale it.
      expect(frameWidth()).toMatch(/^min\(.*, 768px\)$/);
    });

    it('still scales down to fit a narrower or shorter rail', async () => {
      await openPanel(signIn());

      expect(frameWidth()).toContain('100cqw');
      expect(frameWidth()).toContain('calc(100cqh * 0.9231)');
    });

    it('widens the rail to fit the stream, and restores it on close', async () => {
      windowWidth(1600);
      expect(docked.width()).toBe(672);

      const el = await openPanel(signIn());
      expect(docked.width()).toBe(768 + 32);

      (el.querySelector('[aria-label="Close browser panel"]') as HTMLButtonElement).click();
      fixture.detectChanges();
      expect(docked.width()).toBe(672);
    });

    it('takes only what the window can spare above the chat floor', async () => {
      windowWidth(1100);

      await openPanel(signIn());

      expect(docked.width()).toBe(1100 - 360);
    });

    it('leaves the rail alone when the window has no room to widen it', async () => {
      windowWidth(1000);

      await openPanel(signIn());

      expect(docked.width()).toBe(672);
    });

    it('never narrows a rail the user already made wider', async () => {
      windowWidth(1600);
      docked.setWidth(1000);

      await openPanel(signIn());
      expect(docked.width()).toBe(1000);

      pane.close();
      fixture.detectChanges();
      expect(docked.width()).toBe(1000);
    });

    it('keeps a width the user dragged to while it was open', async () => {
      windowWidth(1600);
      await openPanel(signIn());

      docked.setWidth(900);
      pane.close();
      fixture.detectChanges();

      expect(docked.width()).toBe(900);
    });

    it('gives the width back when another pane takes the rail', async () => {
      windowWidth(1600);
      await openPanel(signIn());

      docked.claim('artifact');
      TestBed.tick();

      expect(docked.width()).toBe(672);
    });
  });

  describe('expanding to the full window', () => {
    function toggle(el: HTMLElement): HTMLButtonElement {
      return el.querySelector(
        '[aria-label="Expand browser to full window"], [aria-label="Return browser to the side panel"]',
      ) as HTMLButtonElement;
    }

    it('restyles the pane in place, so the stream is never reloaded', async () => {
      const el = await openPanel();
      const frame = el.querySelector('iframe');

      toggle(el).click();
      fixture.detectChanges();
      const aside = el.querySelector('aside')!;
      expect(aside.getAttribute('role')).toBe('dialog');
      expect(aside.getAttribute('aria-modal')).toBe('true');
      expect(aside.style.maxWidth).toBe('');
      // The same element: moving or re-creating it would drop the DCV stream.
      expect(el.querySelector('iframe')).toBe(frame);

      toggle(el).click();
      fixture.detectChanges();
      expect(aside.getAttribute('role')).toBeNull();
      expect(el.querySelector('iframe')).toBe(frame);
      expect(service.mintLiveView).toHaveBeenCalledTimes(1);
    });

    it('is a labelled toggle a keyboard user can reach', async () => {
      const el = await openPanel();
      const button = toggle(el);

      expect(button.tagName).toBe('BUTTON');
      expect(button.getAttribute('aria-label')).toBe('Expand browser to full window');
      button.click();
      fixture.detectChanges();
      expect(toggle(el).getAttribute('aria-label')).toBe(
        'Return browser to the side panel',
      );
    });

    it('collapses on Escape but never closes the pane', async () => {
      const el = await openPanel();
      toggle(el).click();
      fixture.detectChanges();

      el.querySelector('aside')!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
      fixture.detectChanges();

      expect(el.querySelector('aside')!.getAttribute('role')).toBeNull();
      expect(docked.owner()).toBe('browser');
      expect(el.querySelector('iframe')).not.toBeNull();
    });

    it('does nothing on Escape while docked', async () => {
      const el = await openPanel();

      el.querySelector('aside')!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
      fixture.detectChanges();

      expect(docked.owner()).toBe('browser');
      expect(service.complete).not.toHaveBeenCalled();
    });
  });

  describe('the ready handshake', () => {
    function stubFrame(el: HTMLElement) {
      const post = vi.fn();
      Object.defineProperty(el.querySelector('iframe')!, 'contentWindow', {
        value: { postMessage: post },
        configurable: true,
      });
      return post;
    }

    it('ignores a ready message from any other origin', async () => {
      const post = stubFrame(await openPanel());

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
      const post = stubFrame(await openPanel());

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

  describe('closing', () => {
    it('reports completion for this interrupt and gives the rail back', async () => {
      const el = await openPanel();
      const done = Array.from(el.querySelectorAll('button')).find((b) =>
        b.textContent?.includes("I've signed in"),
      ) as HTMLButtonElement;
      done.click();
      await fixture.whenStable();

      expect(service.complete).toHaveBeenCalledWith('v1:tool_call:tu-1:abc');
      expect(docked.isOpen()).toBe(false);
    });

    it('closes without resolving the prompt, so the card can reopen it', async () => {
      const el = await openPanel();
      (el.querySelector('[aria-label="Close browser panel"]') as HTMLButtonElement).click();
      fixture.detectChanges();

      expect(el.querySelector('iframe')).toBeNull();
      expect(service.complete).not.toHaveBeenCalled();
      expect(docked.isOpen()).toBe(false);
    });

    it('gives the rail back once the prompt is resolved elsewhere', async () => {
      await openPanel();
      service.pending.set([]);
      TestBed.tick();

      expect(docked.isOpen()).toBe(false);
    });

    it('closes when the sign-in window lapses', async () => {
      await openPanel();
      // Driven through the request signal rather than the clock: the ticker
      // is a real setInterval created when the pane opens.
      service.hasLapsed.mockReturnValue(true);
      service.pending.set([request({ toolUseId: 'tu-2' })]);
      TestBed.tick();

      expect(docked.isOpen()).toBe(false);
    });
  });
});
