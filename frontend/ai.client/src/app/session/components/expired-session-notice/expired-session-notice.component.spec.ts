import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { ExpiredSessionNoticeComponent } from './expired-session-notice.component';

describe('ExpiredSessionNoticeComponent', () => {
  const navigate = vi.fn();

  beforeEach(() => {
    TestBed.resetTestingModule();
    navigate.mockReset();
    TestBed.configureTestingModule({
      imports: [ExpiredSessionNoticeComponent],
      providers: [{ provide: Router, useValue: { navigate } }],
    });
  });

  it('says the conversation is read-only and starts a fresh one', () => {
    const fixture = TestBed.createComponent(ExpiredSessionNoticeComponent);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('[role=status]')?.textContent).toContain('read-only');
    el.querySelector<HTMLButtonElement>('button')!.click();

    expect(navigate).toHaveBeenCalledWith(['']);
  });
});
