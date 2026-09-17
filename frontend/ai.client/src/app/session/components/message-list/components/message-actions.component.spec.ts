import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { provideMarkdown, MarkdownService } from 'ngx-markdown';
import { MessageActionsComponent } from './message-actions.component';
import { Message, MessageFeedback } from '../../../services/models/message.model';
import { MessageFeedbackService } from '../../../services/session/message-feedback.service';
import { signal } from '@angular/core';

function makeMessage(text: string): Message {
  return {
    id: 'msg-1',
    role: 'assistant',
    content: [{ type: 'text', text }],
  };
}

function continueButton(fixture: ComponentFixture<MessageActionsComponent>): HTMLButtonElement | null {
  return fixture.nativeElement.querySelector(
    'button[aria-label="Continue the truncated response"]',
  );
}

describe('MessageActionsComponent — Continue affordance', () => {
  let fixture: ComponentFixture<MessageActionsComponent>;
  let component: MessageActionsComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MessageActionsComponent],
      providers: [provideMarkdown()],
    }).compileComponents();

    const markdownService = TestBed.inject(MarkdownService);
    markdownService.parse = () => '';

    fixture = TestBed.createComponent(MessageActionsComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('messages', [makeMessage('partial answer')]);
  });

  it('hides the Continue button by default', () => {
    fixture.detectChanges();
    expect(continueButton(fixture)).toBeNull();
  });

  it('shows the Continue button when canContinue is true', () => {
    fixture.componentRef.setInput('canContinue', true);
    fixture.detectChanges();
    expect(continueButton(fixture)).not.toBeNull();
  });

  it('emits continueRequested when the Continue button is clicked', () => {
    fixture.componentRef.setInput('canContinue', true);
    fixture.detectChanges();

    let emitted = 0;
    component.continueRequested.subscribe(() => emitted++);

    continueButton(fixture)!.click();
    expect(emitted).toBe(1);
  });
});

describe('MessageActionsComponent — interrupted-turn chip', () => {
  let fixture: ComponentFixture<MessageActionsComponent>;
  let component: MessageActionsComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MessageActionsComponent],
      providers: [provideMarkdown()],
    }).compileComponents();

    const markdownService = TestBed.inject(MarkdownService);
    markdownService.parse = () => '';

    fixture = TestBed.createComponent(MessageActionsComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('messages', [makeMessage('partial answer')]);
  });

  it('shows "Response interrupted" + a Continue button for connection_lost', () => {
    fixture.componentRef.setInput('interruptedReason', 'connection_lost');
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Response interrupted');
    const btn = fixture.nativeElement.querySelector(
      'button[aria-label="Continue the interrupted response"]',
    );
    expect(btn).not.toBeNull();
  });

  it('emits continueRequested when the interrupted Continue button is clicked', () => {
    fixture.componentRef.setInput('interruptedReason', 'connection_lost');
    fixture.detectChanges();

    let emitted = 0;
    component.continueRequested.subscribe(() => emitted++);
    fixture.nativeElement
      .querySelector('button[aria-label="Continue the interrupted response"]')!
      .click();
    expect(emitted).toBe(1);
  });

  it('shows "You stopped this response" with NO Continue for user_stopped', () => {
    fixture.componentRef.setInput('interruptedReason', 'user_stopped');
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('You stopped this response');
    expect(
      fixture.nativeElement.querySelector('button[aria-label="Continue the interrupted response"]'),
    ).toBeNull();
  });

  it('max_tokens Continue takes precedence over an interrupted reason', () => {
    fixture.componentRef.setInput('canContinue', true);
    fixture.componentRef.setInput('interruptedReason', 'connection_lost');
    fixture.detectChanges();

    // The truncated-turn label wins; the interrupted chip is not also shown.
    expect(fixture.nativeElement.textContent).toContain('Response length limit reached');
    expect(fixture.nativeElement.textContent).not.toContain('Response interrupted');
  });
});

/** Duck-typed MessageFeedbackService: records calls, serves a fixed value. */
class FakeFeedbackService {
  unavailable = signal(false);
  isPending = signal(() => false);
  current: MessageFeedback | null = null;
  set: Array<{ id: string; value: 1 | -1; reason?: string }> = [];
  cleared: string[] = [];
  retried: string[] = [];
  requestRetry(message: Message): void {
    this.retried.push(message.id);
  }
  feedbackFor(): MessageFeedback | null {
    return this.current;
  }
  async setFeedback(message: Message, value: 1 | -1, reason?: string): Promise<void> {
    this.set.push({ id: message.id, value, reason });
  }
  async clearFeedback(message: Message): Promise<void> {
    this.cleared.push(message.id);
  }
}

function serverMessage(text: string, id = 'msg-sess-1-3'): Message {
  return { id, role: 'assistant', content: [{ type: 'text', text }] };
}

describe('MessageActionsComponent — thumbs feedback', () => {
  let fixture: ComponentFixture<MessageActionsComponent>;
  let feedback: FakeFeedbackService;

  const up = () => fixture.nativeElement.querySelector('button[aria-label="Good response"], button[aria-label="Remove thumbs up"]') as HTMLButtonElement | null;
  const down = () => fixture.nativeElement.querySelector('button[aria-label="Bad response"], button[aria-label="Remove thumbs down"]') as HTMLButtonElement | null;

  beforeEach(async () => {
    feedback = new FakeFeedbackService();
    await TestBed.configureTestingModule({
      imports: [MessageActionsComponent],
      providers: [provideMarkdown(), { provide: MessageFeedbackService, useValue: feedback }],
    }).compileComponents();
    TestBed.inject(MarkdownService).parse = () => '';
    fixture = TestBed.createComponent(MessageActionsComponent);
    fixture.componentRef.setInput('messages', [serverMessage('answer')]);
  });

  it('renders an unpressed thumbs pair with tooltips for a server-shaped message', () => {
    fixture.detectChanges();
    expect(up()).not.toBeNull();
    expect(down()).not.toBeNull();
    expect(up()!.getAttribute('aria-pressed')).toBe('false');
    expect(up()!.getAttribute('aria-label')).toBe('Good response');
    // No free-text input anywhere in the affordance.
    expect(fixture.nativeElement.querySelector('input, textarea')).toBeNull();
  });

  it('hides the pair for a message whose id carries no server index', () => {
    fixture.componentRef.setInput('messages', [serverMessage('x', 'placeholder')]);
    fixture.detectChanges();
    expect(up()).toBeNull();
  });

  it('hides the pair once the service reports the surface unavailable', () => {
    feedback.unavailable.set(true);
    fixture.detectChanges();
    expect(up()).toBeNull();
  });

  it('thumbs up sets +1 on the run\'s last message', () => {
    fixture.componentRef.setInput('messages', [serverMessage('a', 'msg-sess-1-2'), serverMessage('b', 'msg-sess-1-3')]);
    fixture.detectChanges();
    up()!.click();
    expect(feedback.set).toEqual([{ id: 'msg-sess-1-3', value: 1, reason: undefined }]);
  });

  it('clicking the pressed thumb withdraws it', () => {
    feedback.current = { value: 1, updatedAt: 't' };
    fixture.detectChanges();
    expect(up()!.getAttribute('aria-pressed')).toBe('true');
    expect(up()!.getAttribute('aria-label')).toBe('Remove thumbs up');
    up()!.click();
    expect(feedback.cleared).toEqual(['msg-sess-1-3']);
    expect(feedback.set).toEqual([]);
  });

  it('a thumbs down reveals the reason codes and a pick re-sends with the code', () => {
    feedback.current = { value: -1, updatedAt: 't' };
    fixture.detectChanges();
    const group = fixture.nativeElement.querySelector('[role="group"]');
    expect(group).not.toBeNull();
    // Reason chips are the pressable ones; the Retry button shares the group but is not a reason.
    const chips = Array.from(group.querySelectorAll('button[aria-pressed]')) as HTMLButtonElement[];
    expect(chips.map((c) => c.textContent!.trim())).toEqual([
      'Wrong or made up',
      'Ignored instructions',
      'Too long / short',
      'A tool failed',
      'Out of date',
      'Something else',
    ]);
    chips[1].click();
    expect(feedback.set).toEqual([{ id: 'msg-sess-1-3', value: -1, reason: 'instructions' }]);
  });

  it('a thumbs down offers "Retry with that in mind", which asks the service to draft a correction', () => {
    feedback.current = { value: -1, reason: 'wrong', updatedAt: 't' };
    fixture.detectChanges();
    const retry = fixture.nativeElement.querySelector('button[aria-label="Retry with that in mind"]') as HTMLButtonElement;
    expect(retry).not.toBeNull();
    retry.click();
    expect(feedback.retried).toEqual(['msg-sess-1-3']);
    expect(feedback.set).toEqual([]);
  });

  it('reason codes stay hidden on a thumbs up', () => {
    feedback.current = { value: 1, updatedAt: 't' };
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="group"]')).toBeNull();
    expect(fixture.nativeElement.querySelector('button[aria-label="Retry with that in mind"]')).toBeNull();
  });
});
