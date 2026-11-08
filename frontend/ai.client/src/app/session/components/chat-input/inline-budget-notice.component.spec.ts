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

  it('names a document that will be read from storage instead', () => {
    expect(render({ byReference: [file('policy-d.pdf')], dropped: [] })).toBe(
      'These attachments are over the 7 MB one message can include in full. ' +
        'policy-d.pdf will be read from storage instead, in the parts the answer needs.',
    );
  });

  it('names images that need a follow-up message', () => {
    expect(render({ byReference: [], dropped: [file('a.png', 'image/png'), file('b.png', 'image/png')] })).toBe(
      'These attachments are over the 7 MB one message can include in full. ' +
        "a.png and b.png won't be included. Send them in a follow-up message.",
    );
  });

  it('says both when both happen', () => {
    const text = render({
      byReference: [file('one.pdf'), file('two.pdf'), file('three.docx')],
      dropped: [file('photo.png', 'image/png')],
    });

    expect(text).toContain('one.pdf, two.pdf and three.docx will be read from storage instead');
    expect(text).toContain("photo.png won't be included. Send it in a follow-up message.");
  });
});
