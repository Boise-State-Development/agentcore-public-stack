import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NO_ERRORS_SCHEMA, signal } from '@angular/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UserMessageComponent } from './user-message.component';
import { LocalSettingsService } from '../../../../services/local-settings.service';
import type { Message } from '../../../services/models/message.model';
import { FailedSendService } from '../../../services/chat/failed-send.service';

/**
 * The "Show more" affordance on a user bubble is driven by ONE measurement of
 * the content wrapper. It used to be taken once in `ngAfterViewInit` and never
 * revisited, so a reading taken before layout settled latched forever: a
 * one-line mid-turn steer rendered a "Show more" and a fade dimming its own
 * single line, on dev, permanently.
 *
 * jsdom has no layout engine, so `scrollHeight` is whatever we say it is —
 * which is exactly what makes the *sequence* testable: an early wrong reading
 * followed by a correct one must end at the correct answer.
 */
describe('UserMessageComponent — overflow measurement', () => {
  let fixture: ComponentFixture<UserMessageComponent>;
  let component: UserMessageComponent;
  let observed: Element | null;
  let fireResize: () => void;

  const message = (text: string, steering = false): Message => ({
    id: 'msg-1',
    role: 'user',
    content: [{ type: 'text', text }],
    ...(steering ? { steering: true } : {}),
  });

  /** Pretend the wrapper measures `height` px of content. */
  function setScrollHeight(height: number): void {
    const wrapper = fixture.nativeElement.querySelector('.overflow-hidden');
    Object.defineProperty(wrapper, 'scrollHeight', { value: height, configurable: true });
  }

  beforeEach(async () => {
    observed = null;
    fireResize = () => undefined;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fireResize = cb;
        }
        observe(el: Element) {
          observed = el;
        }
        disconnect() {
          observed = null;
        }
      },
    );

    await TestBed.configureTestingModule({
      imports: [UserMessageComponent],
      providers: [
        { provide: LocalSettingsService, useValue: { showDebugOutput: signal(false) } },
      ],
    })
      .overrideComponent(UserMessageComponent, {
        set: { imports: [], schemas: [NO_ERRORS_SCHEMA] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(UserMessageComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('message', message('a short follow-up'));
    fixture.detectChanges();
  });

  it('observes the content wrapper rather than measuring once', () => {
    expect(observed).not.toBeNull();
    expect((observed as unknown as Element).classList.contains('overflow-hidden')).toBe(true);
  });

  it('corrects an early mis-measurement once the box settles', () => {
    // The bug, in sequence: an unsettled container wraps a short line into
    // dozens of wrapped ones...
    setScrollHeight(960);
    fireResize();
    fixture.detectChanges();
    expect(component.isOverflowing()).toBe(true);

    // ...and then layout settles and it is one line again. Before the fix
    // nothing measured a second time, so the bubble kept a "Show more" and a
    // fade over its own single line forever.
    setScrollHeight(24);
    fireResize();
    fixture.detectChanges();

    expect(component.isOverflowing()).toBe(false);
    expect(fixture.nativeElement.querySelector('button')).toBeNull();
  });

  it('still shows the affordance for genuinely long content', () => {
    setScrollHeight(960);
    fireResize();
    fixture.detectChanges();

    expect(component.isOverflowing()).toBe(true);
    expect(fixture.nativeElement.querySelector('button').textContent.trim()).toBe('Show more');
  });

  it('gives a steering message the same bubble as any other user message', () => {
    const bubbleOf = (steering: boolean) => {
      const f = TestBed.createComponent(UserMessageComponent);
      f.componentRef.setInput('message', message('hello', steering));
      f.detectChanges();
      return f.nativeElement.querySelector('.rounded-2xl').className;
    };
    // The caption is the only distinction; a second visual treatment made a
    // steer read as a different kind of object rather than the same kind of
    // message sent at an unusual moment.
    expect(bubbleOf(true)).toBe(bubbleOf(false));
  });

  it('disconnects the observer on destroy', () => {
    fixture.destroy();
    expect(observed).toBeNull();
  });
});

describe('UserMessageComponent — a message that was not sent', () => {
  // Dev, 2026-10-09: a follow-up refused before anything streamed left a
  // bubble that looked delivered, with no reply under it and no way to resend.
  let failedSends: { canRetry: ReturnType<typeof vi.fn>; retry: ReturnType<typeof vi.fn> };

  const notSent = (): Message => ({
    id: 'msg-s1-6',
    role: 'user',
    content: [{ type: 'text', text: 'and the third one?' }],
    sendFailure: { reason: 'Your sign-in was being renewed when this was sent.' },
  });

  async function render(message: Message, canRetry = true): Promise<HTMLElement> {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    failedSends = { canRetry: vi.fn().mockReturnValue(canRetry), retry: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [UserMessageComponent],
      providers: [
        { provide: LocalSettingsService, useValue: { showDebugOutput: signal(false) } },
        { provide: FailedSendService, useValue: failedSends },
      ],
    })
      .overrideComponent(UserMessageComponent, { set: { imports: [], schemas: [NO_ERRORS_SCHEMA] } })
      .compileComponents();
    const fixture = TestBed.createComponent(UserMessageComponent);
    fixture.componentRef.setInput('message', message);
    fixture.detectChanges();
    return fixture.nativeElement;
  }

  it('says so on the message, with the reason, and offers a retry', async () => {
    const el = await render(notSent());

    const alert = el.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('Not sent.');
    expect(alert?.textContent).toContain('Your sign-in was being renewed when this was sent.');

    const retry = alert?.querySelector('button') as HTMLButtonElement;
    expect(retry.textContent?.trim()).toBe('Retry');
    retry.click();
    expect(failedSends.retry).toHaveBeenCalledWith('msg-s1-6');
  });

  it('hides the retry while a resend could not start a turn', async () => {
    const el = await render(notSent(), false);

    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Not sent.');
    expect(el.querySelector('[role="alert"] button')).toBeNull();
  });

  it('a delivered message carries no notice', async () => {
    const delivered = { ...notSent(), sendFailure: undefined };
    const el = await render(delivered);

    expect(el.querySelector('[role="alert"]')).toBeNull();
  });
});
