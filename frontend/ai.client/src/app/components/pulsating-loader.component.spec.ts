import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, it, expect } from 'vitest';
import { PulsatingLoaderComponent } from './pulsating-loader.component';

@Component({
  imports: [PulsatingLoaderComponent],
  template: `<app-pulsating-loader [notice]="notice" />`,
})
class HostComponent {
  notice: string | null = null;
}

function render(notice: string | null) {
  const fixture = TestBed.createComponent(HostComponent);
  fixture.componentInstance.notice = notice;
  fixture.detectChanges();
  return fixture;
}

describe('PulsatingLoaderComponent', () => {
  it('cycles its own phrases when no notice is set', () => {
    const fixture = render(null);
    const loader = fixture.debugElement.children[0].componentInstance as PulsatingLoaderComponent;
    // The typewriter starts empty and fills in; what matters is that it is not
    // showing a caller-supplied string.
    expect(loader.displayText()).not.toContain('Retrying');
  });

  it('shows the notice verbatim instead of a loading phrase', () => {
    const fixture = render('The model is busy. Retrying…');
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('The model is busy. Retrying');
  });

  it('drops the typing cursor for a notice', () => {
    // The cursor reads as "still typing" on text that is finished.
    const withNotice = render('Still working…');
    expect((withNotice.nativeElement as HTMLElement).querySelector('.typing-cursor')).toBeNull();

    const withoutNotice = render(null);
    expect(
      (withoutNotice.nativeElement as HTMLElement).querySelector('.typing-cursor'),
    ).not.toBeNull();
  });

  it('marks the indicator dot so the change is visible peripherally', () => {
    const fixture = render('Still working…');
    const dot = (fixture.nativeElement as HTMLElement).querySelector('.pulsing-circle');
    expect(dot?.classList.contains('is-notice')).toBe(true);
  });

  it('announces a notice to assistive tech', () => {
    const fixture = render('Still working…');
    const status = (fixture.nativeElement as HTMLElement).querySelector('[role="status"]');
    expect(status?.getAttribute('aria-live')).toBe('polite');
    expect(status?.getAttribute('aria-label')).toContain('Still working');
  });
});
