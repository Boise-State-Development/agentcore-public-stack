import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import type { BudgetedAttachment, InlineBudgetOverflow } from '../../../services/file-upload';
import { InlineBudgetNoticeComponent } from './inline-budget-notice.component';

function file(filename: string, mimeType = 'application/pdf'): BudgetedAttachment {
  return { id: filename, filename, mimeType, sizeBytes: 3_000_000 };
}

describe('InlineBudgetNoticeComponent', () => {
  let fixture: ComponentFixture<InlineBudgetNoticeComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [InlineBudgetNoticeComponent] }).compileComponents();
    fixture = TestBed.createComponent(InlineBudgetNoticeComponent);
  });

  function render(overflow: InlineBudgetOverflow | null): string {
    fixture.componentRef.setInput('overflow', overflow);
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement).textContent?.replace(/\s+/g, ' ').trim() ?? '';
  }

  it('is an empty polite live region while everything fits', () => {
    expect(render(null)).toBe('');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.getAttribute('role')).toBe('status');
    expect(host.getAttribute('aria-live')).toBe('polite');
  });

  it('names a document read on demand in a quiet line, not a warning', () => {
    expect(render({ byReference: [file('policy-d.pdf')], dropped: [] })).toBe(
      "policy-d.pdf will be read on demand rather than included in full, since this message's files " +
        'together are over 7 MB. Ask about it by name if the answer leaves it out.',
    );
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('[data-testid="inline-budget-on-demand"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="inline-budget-notice"]')).toBeNull();
  });

  it('names images that need a follow-up message', () => {
    expect(render({ byReference: [], dropped: [file('a.png', 'image/png'), file('b.png', 'image/png')] })).toBe(
      "a.png and b.png won't fit in this message, which can include 7 MB of images and documents in full. " +
        'Send them in a follow-up message.',
    );
  });

  it('says both, each in its own tone, when both happen', () => {
    const text = render({
      byReference: [file('one.pdf'), file('two.pdf'), file('three.docx')],
      dropped: [file('photo.png', 'image/png')],
    });

    expect(text).toContain('one.pdf, two.pdf and three.docx will be read on demand');
    expect(text).toContain('Ask about them by name if the answer leaves them out.');
    expect(text).toContain("photo.png won't fit in this message");
    expect(text).toContain('Send it in a follow-up message.');
    const warning = (fixture.nativeElement as HTMLElement).querySelector('[data-testid="inline-budget-notice"]');
    expect(warning?.textContent).not.toContain('one.pdf');
  });
});
