import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { ArtifactSourceComponent } from './artifact-source.component';

/**
 * Prism is loaded as a global angular.json script, absent under vitest,
 * so `getPrism()` returns null and the component falls back to escaped
 * plain text. That makes the XSS-safety assertion deterministic here
 * regardless of grammar availability.
 */
describe('ArtifactSourceComponent', () => {
  let fixture: ComponentFixture<ArtifactSourceComponent>;

  function render(content: string, contentType: string): HTMLElement {
    fixture = TestBed.createComponent(ArtifactSourceComponent);
    fixture.componentRef.setInput('content', content);
    fixture.componentRef.setInput('contentType', contentType);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ArtifactSourceComponent],
    });
  });

  it('renders one gutter number per source line', () => {
    const el = render('a\nb\nc', 'text/css');
    const gutter = el.querySelectorAll('[aria-hidden="true"] > div');
    expect(gutter.length).toBe(3);
    expect(Array.from(gutter).map((d) => d.textContent)).toEqual([
      '1',
      '2',
      '3',
    ]);
  });

  it('counts a trailing newline as a final (empty) line', () => {
    const el = render('only\n', 'text/css');
    expect(el.querySelectorAll('[aria-hidden="true"] > div').length).toBe(2);
  });

  it('maps the content type onto a Prism language class', () => {
    const el = render('<p>x</p>', 'text/html; charset=utf-8');
    expect(el.querySelector('code')?.className).toContain(
      'language-markup',
    );
  });

  it('falls back to a plain-text language for unknown types', () => {
    const el = render('plain', 'application/octet-stream');
    expect(el.querySelector('code')?.className).toContain('language-none');
  });

  it('escapes HTML so artifact source can never execute', () => {
    const el = render('<script>alert(1)</script>', 'text/html');
    const code = el.querySelector('code');
    // No live <script> element was injected…
    expect(code?.querySelector('script')).toBeNull();
    // …the angle brackets were rendered as text instead.
    expect(code?.innerHTML).toContain('&lt;script&gt;');
    expect(code?.textContent).toBe('<script>alert(1)</script>');
  });
});
