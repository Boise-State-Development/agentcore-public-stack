import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { AnnouncementsService } from '../../services/announcements/announcements.service';
import { Announcement } from '../../services/announcements/announcement.model';
import { AnnouncementBannerComponent } from './announcement-banner.component';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Skills are here',
    body_markdown: '# Skills\n\n- one',
    summary: null,
    surfaces: ['panel', 'banner'],
    severity: 'info',
    publish_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-02-01T00:00:00Z',
    requires_ack: false,
    cta_label: null,
    cta_url: null,
    revision: 1,
    is_unread: true,
    is_updated: false,
    ...overrides,
  };
}

describe('AnnouncementBannerComponent', () => {
  let bannerItem: ReturnType<typeof signal<Announcement | null>>;
  let ack: ReturnType<typeof vi.fn>;
  let observed: Element[];

  beforeEach(() => {
    TestBed.resetTestingModule();
    bannerItem = signal<Announcement | null>(null);
    ack = vi.fn(async () => true);
    observed = [];

    // jsdom has no ResizeObserver. Stub one that records what it watches so
    // the height-publishing path is actually exercised rather than skipped.
    (globalThis as any).ResizeObserver = class {
      constructor(private cb: ResizeObserverCallback) {}
      observe(el: Element) {
        observed.push(el);
      }
      disconnect() {}
      unobserve() {}
      emit(height: number) {
        this.cb(
          [{ contentRect: { height } } as unknown as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        );
      }
    };

    // DI-token override rather than vi.mock, per house convention.
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsService, useValue: { bannerItem, ack } },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    document.documentElement.style.removeProperty('--announcement-banner-height');
  });

  function create() {
    const fixture = TestBed.createComponent(AnnouncementBannerComponent);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ReturnType<typeof create>) {
    return (fixture.nativeElement as HTMLElement).textContent?.trim() ?? '';
  }

  function strip(fixture: ReturnType<typeof create>): HTMLElement | null {
    return (fixture.nativeElement as HTMLElement).querySelector('[role="status"]');
  }

  it('renders nothing when the server sent no banner', () => {
    const fixture = create();
    expect(strip(fixture)).toBeNull();
  });

  it('renders the strip once the feed resolves', () => {
    // The dependency-set regression from #974: read the derived state while
    // the feed is still EMPTY first, then populate it. A computed that
    // guard-clauses out before touching its signal tracks nothing on that
    // first evaluation and never recomputes — which is how the submit button
    // on the admin form shipped permanently disabled.
    const fixture = create();
    const component = fixture.componentInstance;
    expect(component.bannerText()).toBe('');
    expect(strip(fixture)).toBeNull();

    bannerItem.set(makeAnnouncement());
    fixture.detectChanges();

    expect(component.bannerText()).toBe('Skills are here');
    expect(strip(fixture)).not.toBeNull();
    expect(text(fixture)).toContain('Skills are here');
  });

  it('prefers the summary over the title — the strip is one line', () => {
    bannerItem.set(
      makeAnnouncement({ title: 'A'.repeat(140), summary: 'Short version' }),
    );
    const fixture = create();
    expect(text(fixture)).toContain('Short version');
    expect(text(fixture)).not.toContain('A'.repeat(140));
  });

  it('falls back to the title when the summary is blank whitespace', () => {
    bannerItem.set(makeAnnouncement({ summary: '   ' }));
    const fixture = create();
    expect(fixture.componentInstance.bannerText()).toBe('Skills are here');
  });

  it('writes `seen` on render, and only once per announcement', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();

    expect(ack).toHaveBeenCalledWith('a1', 'seen', 'banner');
    expect(ack).toHaveBeenCalledTimes(1);

    fixture.detectChanges();
    fixture.detectChanges();
    expect(ack).toHaveBeenCalledTimes(1);
  });

  it('writes `seen` again when a different announcement takes the slot', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    ack.mockClear();

    bannerItem.set(makeAnnouncement({ announcement_id: 'a2' }));
    fixture.detectChanges();

    expect(ack).toHaveBeenCalledWith('a2', 'seen', 'banner');
  });

  it('records a durable `dismissed` ack on ✕, scoped to the banner surface', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    ack.mockClear();

    const dismiss = (fixture.nativeElement as HTMLElement).querySelector(
      'button',
    ) as HTMLButtonElement;
    expect(dismiss.getAttribute('aria-label')).toBe(
      'Dismiss announcement: Skills are here',
    );
    dismiss.click();

    expect(ack).toHaveBeenCalledWith('a1', 'dismissed', 'banner');
  });

  it('is a polite live region, never assertive', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    const el = strip(fixture)!;
    expect(el.getAttribute('aria-live')).toBe('polite');
    expect(el.getAttribute('role')).toBe('status');
  });

  it.each([
    ['info' as const, 'heroInformationCircle', 'state-info'],
    ['success' as const, 'heroCheckCircle', 'state-success'],
    ['warning' as const, 'heroExclamationTriangle', 'state-warning'],
  ])('maps %s severity to its icon and token scale', (severity, icon, scale) => {
    bannerItem.set(makeAnnouncement({ severity }));
    const fixture = create();
    const component = fixture.componentInstance;

    expect(component.iconName()).toBe(icon);
    // Full literal class strings — a concatenated `bg-state-${severity}-50`
    // would compile to nothing, because Tailwind scans source text.
    expect(component.severityClass()).toContain(`bg-${scale}-50`);
    expect(component.severityClass()).toContain(`dark:bg-${scale}-900/30`);
  });

  it('renders the CTA only when both label and url are present', () => {
    bannerItem.set(makeAnnouncement({ cta_label: 'Read more' }));
    let fixture = create();
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('a'),
    ).toBeNull();

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsService, useValue: { bannerItem, ack } },
      ],
    });
    bannerItem.set(
      makeAnnouncement({ cta_label: 'Read more', cta_url: 'https://example.edu' }),
    );
    fixture = create();
    const link = (fixture.nativeElement as HTMLElement).querySelector('a')!;
    expect(link.getAttribute('href')).toBe('https://example.edu');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('publishes its measured height so the fixed topnav can clear it', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();

    expect(observed).toHaveLength(1);
    expect(observed[0]).toBe(fixture.nativeElement);
  });

  it('clears the height variable on destroy, so nothing is left offset', () => {
    bannerItem.set(makeAnnouncement());
    const fixture = create();
    expect(
      document.documentElement.style.getPropertyValue(
        '--announcement-banner-height',
      ),
    ).not.toBe('');

    fixture.destroy();

    expect(
      document.documentElement.style.getPropertyValue(
        '--announcement-banner-height',
      ),
    ).toBe('');
  });
});
