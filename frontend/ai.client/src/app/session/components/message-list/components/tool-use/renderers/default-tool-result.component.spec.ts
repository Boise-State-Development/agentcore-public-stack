import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { DefaultToolResultComponent } from './default-tool-result.component';
import { ToolResultData } from '../tool-renderer-registry.service';

function makeResult(content: ToolResultData['content']): ToolResultData {
  return { status: 'success', content };
}

describe('DefaultToolResultComponent', () => {
  let fixture: ComponentFixture<DefaultToolResultComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [DefaultToolResultComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(DefaultToolResultComponent);
  });

  it('renders plain text content', () => {
    fixture.componentRef.setInput('result', makeResult([{ text: 'hello world' }]));
    fixture.detectChanges();

    const textEl = fixture.nativeElement.querySelector('.whitespace-pre-wrap');
    expect(textEl).toBeTruthy();
    expect(textEl.textContent).toContain('hello world');
  });

  it('renders JSON content as a highlighted code block', () => {
    fixture.componentRef.setInput('result', makeResult([{ json: { answer: 42 } }]));
    fixture.detectChanges();

    const codeEl = fixture.nativeElement.querySelector('pre code');
    expect(codeEl).toBeTruthy();
    expect(codeEl.textContent).toContain('answer');
    expect(codeEl.textContent).toContain('42');
    // The plain-text branch must not be used for JSON items.
    expect(fixture.nativeElement.querySelector('.whitespace-pre-wrap')).toBeNull();
  });

  it('renders image content in full (non-minimized) mode', () => {
    fixture.componentRef.setInput(
      'result',
      makeResult([{ image: { format: 'png', data: 'iVBORw0KGgo=' } }]),
    );
    fixture.detectChanges();

    const img = fixture.nativeElement.querySelector('img');
    expect(img).toBeTruthy();
    expect(img.getAttribute('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
  });

  it('uses rounded-sm cards in full mode', () => {
    fixture.componentRef.setInput('result', makeResult([{ text: 'x' }]));
    fixture.detectChanges();

    const card = fixture.nativeElement.querySelector('.space-y-2 > div');
    expect(card.classList.contains('rounded-sm')).toBe(true);
    expect(card.classList.contains('rounded-xs')).toBe(false);
  });

  it('omits images and uses rounded-xs cards in minimized mode', () => {
    fixture.componentRef.setInput(
      'result',
      makeResult([
        { text: 'visible text' },
        { image: { format: 'png', data: 'abc' } },
      ]),
    );
    fixture.componentRef.setInput('minimized', true);
    fixture.detectChanges();

    // Image is suppressed in the minimized variant (historical behavior).
    expect(fixture.nativeElement.querySelector('img')).toBeNull();

    const card = fixture.nativeElement.querySelector('.space-y-2 > div');
    expect(card.classList.contains('rounded-xs')).toBe(true);
    expect(card.classList.contains('rounded-sm')).toBe(false);
    expect(card.textContent).toContain('visible text');
  });
});
