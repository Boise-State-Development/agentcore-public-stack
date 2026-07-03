import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { provideMarkdown, MarkdownService } from 'ngx-markdown';
import { MessageActionsComponent } from './message-actions.component';
import { Message } from '../../../services/models/message.model';

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
    fixture.componentRef.setInput('message', makeMessage('partial answer'));
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
    fixture.componentRef.setInput('message', makeMessage('partial answer'));
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
