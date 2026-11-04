import { describe, it, expect } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ConnectorLogoComponent } from './connector-logo.component';
import { getBrandIconDataUrl } from '../models/connector-brand-icons';

function render(inputs: Record<string, unknown>) {
  TestBed.resetTestingModule();
  const fixture = TestBed.createComponent(ConnectorLogoComponent);
  for (const [key, value] of Object.entries(inputs)) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('ConnectorLogoComponent', () => {
  it('shows the brand logo for a vendor that has one', () => {
    const img = render({ type: 'github' }).querySelector('img');
    expect(img?.getAttribute('src')).toBe(getBrandIconDataUrl('github'));
  });

  it('prefers an uploaded icon over the brand logo', () => {
    const upload = 'data:image/png;base64,AAAA';
    const img = render({ type: 'github', iconData: upload }).querySelector('img');
    expect(img?.getAttribute('src')).toBe(upload);
  });

  it('falls back to the glyph when there is no image at all', () => {
    const el = render({ type: 'onelogin', iconName: 'heroLink' });
    expect(el.querySelector('img')).toBeNull();
    expect(el.querySelector('ng-icon')).not.toBeNull();
  });

  it('is decorative: the tile is hidden from assistive tech', () => {
    const el = render({ type: 'github' });
    expect(el.querySelector('span')?.getAttribute('aria-hidden')).toBe('true');
    expect(el.querySelector('img')?.getAttribute('alt')).toBe('');
  });
});
