import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';

import { BrowserLoginPromptComponent } from './browser-login-prompt.component';
import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../../../services/browser-login/browser-login.service';
import { BrowserPaneStateService } from '../../../../services/browser-pane/browser-pane-state.service';

const SANDBOX = 'https://mcp-sandbox.example.test';

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
    pending: ReturnType<typeof signal<BrowserLoginRequest[]>>;
    mintLiveView: ReturnType<typeof vi.fn>;
    hasLapsed: ReturnType<typeof vi.fn>;
    complete: ReturnType<typeof vi.fn>;
    skip: ReturnType<typeof vi.fn>;
  };
  let pane: BrowserPaneStateService;

  beforeEach(async () => {
    service = {
      pending: signal<BrowserLoginRequest[]>([]),
      mintLiveView: vi.fn(),
      hasLapsed: vi.fn().mockReturnValue(false),
      complete: vi.fn().mockResolvedValue(undefined),
      skip: vi.fn().mockResolvedValue(undefined),
    };

    await TestBed.configureTestingModule({
      imports: [BrowserLoginPromptComponent],
      // DI token over vi.mock, per the repo's testing convention.
      providers: [{ provide: BrowserLoginService, useValue: service }],
    }).compileComponents();

    pane = TestBed.inject(BrowserPaneStateService);
    fixture = TestBed.createComponent(BrowserLoginPromptComponent);
  });

  afterEach(() => fixture.destroy());

  function render(req: BrowserLoginRequest = request()) {
    service.pending.set([req]);
    fixture.componentRef.setInput('request', req);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function button(el: HTMLElement, label: string): HTMLButtonElement | undefined {
    return Array.from(el.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === label,
    );
  }

  /** Overflow menu items render in a CDK overlay, outside the fixture. */
  function openMenu(el: HTMLElement): HTMLElement[] {
    (el.querySelector('[aria-label^="More actions"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    return Array.from(document.querySelectorAll<HTMLElement>('[cdkmenuitem], [role="menuitem"]'));
  }

  describe('what the user is told', () => {
    it("names the site and shows the agent's reason", () => {
      const el = render();

      expect(el.textContent).toContain('Sign in to jstor.org');
      expect(el.textContent).toContain('Sign in to JSTOR so I can check');
    });

    it('says it is waiting on the user, with the time left', () => {
      const el = render();

      expect(el.textContent).toContain('Waiting for you');
      expect(el.textContent).toMatch(/\dm \d+s left/);
    });

    it('offers no viewer when the sandbox origin is not deployed', () => {
      const el = render(request({ sandboxOrigin: '' }));

      expect(el.textContent).toContain("isn't available in this environment");
      expect(button(el, 'Open')).toBeUndefined();
    });

    it('stops offering the viewer once the window has closed', () => {
      service.hasLapsed.mockReturnValue(true);
      const el = render();

      expect(el.textContent).toContain('sign-in window closed');
      expect(button(el, 'Open')).toBeUndefined();
      expect(button(el, 'Dismiss')).toBeDefined();
    });

    it('holds no viewer of its own — the stream lives in the side panel', () => {
      const el = render();
      expect(el.querySelector('iframe')).toBeNull();
    });
  });

  describe('opening', () => {
    it('opens this prompt in the side panel without minting itself', () => {
      const el = render();
      button(el, 'Open')!.click();
      fixture.detectChanges();

      expect(pane.openInterruptId()).toBe('v1:tool_call:tu-1:abc');
      // The panel mints; a second mint here would be a second live credential.
      expect(service.mintLiveView).not.toHaveBeenCalled();
      expect(el.textContent).toContain('Open in side panel');
    });
  });

  describe('resolving', () => {
    it('reports completion for this interrupt from the menu', async () => {
      const el = render();
      const item = openMenu(el).find((b) => b.textContent?.includes("I've signed in"));
      item!.click();
      await fixture.whenStable();

      expect(service.complete).toHaveBeenCalledWith('v1:tool_call:tu-1:abc');
    });

    it('skips when the user declines, closing its panel', async () => {
      const el = render();
      button(el, 'Open')!.click();
      const item = openMenu(el).find((b) => b.textContent?.includes("Don't sign in"));
      item!.click();
      await fixture.whenStable();

      expect(service.skip).toHaveBeenCalledWith('v1:tool_call:tu-1:abc');
      expect(pane.openInterruptId()).toBeNull();
    });

    it('dismisses a lapsed prompt', async () => {
      service.hasLapsed.mockReturnValue(true);
      const el = render();
      button(el, 'Dismiss')!.click();
      await fixture.whenStable();

      expect(service.skip).toHaveBeenCalledWith('v1:tool_call:tu-1:abc');
    });
  });
});
