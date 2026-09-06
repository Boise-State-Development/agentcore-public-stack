import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import {
  ArtifactThumbnailComponent,
  type ArtifactThumbnailSource,
} from './artifact-thumbnail.component';
import { ArtifactHttpService } from '../../session/services/artifacts/artifact-http.service';
import { ArtifactShareService } from '../../session/services/artifacts/artifact-share.service';

const OWNED: ArtifactThumbnailSource = {
  kind: 'owned',
  artifactId: 'a1',
  version: 2,
  sessionId: 'sess-9',
  contentType: 'text/html',
};

describe('ArtifactThumbnailComponent', () => {
  let fixture: ComponentFixture<ArtifactThumbnailComponent>;
  let mockHttp: { mintRenderToken: ReturnType<typeof vi.fn> };
  let mockShares: { mintSharedRenderToken: ReturnType<typeof vi.fn> };

  /** Fires the observed element into view. Null until one is observed. */
  let intersect: (() => void) | null;
  /** Re-runs the size measurement, as a real resize would. */
  let fireResize: () => void;

  const el = () => fixture.nativeElement as HTMLElement;
  const iframe = () => el().querySelector('iframe');
  /** The inert wrapper the frame is scaled inside. */
  const frame = () => el().querySelector('[inert]') as HTMLElement | null;

  function stubObservers(withIntersectionObserver = true): void {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fireResize = cb;
        }
        observe() {}
        disconnect() {}
      },
    );

    if (!withIntersectionObserver) {
      // `typeof IntersectionObserver === 'undefined'` is the branch under
      // test, so the global has to actually be absent rather than stubbed
      // with something falsy.
      vi.stubGlobal('IntersectionObserver', undefined);
      return;
    }

    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(private cb: (entries: { isIntersecting: boolean }[]) => void) {}
        observe() {
          intersect = () => this.cb([{ isIntersecting: true }]);
        }
        disconnect() {
          intersect = null;
        }
      },
    );
  }

  /** Give the host a width, since jsdom lays nothing out. */
  function setHostWidth(px: number): void {
    Object.defineProperty(el(), 'clientWidth', {
      value: px,
      configurable: true,
    });
  }

  async function create(
    source: ArtifactThumbnailSource = OWNED,
  ): Promise<ComponentFixture<ArtifactThumbnailComponent>> {
    fixture = TestBed.createComponent(ArtifactThumbnailComponent);
    fixture.componentRef.setInput('source', source);
    setHostWidth(512);
    fixture.detectChanges(); // runs ngAfterViewInit → wires the observers
    return fixture;
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    intersect = null;
    fireResize = () => undefined;
    stubObservers();

    mockHttp = {
      mintRenderToken: vi.fn().mockResolvedValue({
        url: 'https://artifacts.example/a1?t=jwt',
        expiresAt: '2026-09-05T00:02:00+00:00',
      }),
    };
    mockShares = {
      mintSharedRenderToken: vi.fn().mockResolvedValue({
        url: 'https://artifacts.example/shared?t=jwt',
        expiresAt: '2026-09-05T00:02:00+00:00',
      }),
    };

    TestBed.configureTestingModule({
      imports: [ArtifactThumbnailComponent],
      providers: [
        { provide: ArtifactHttpService, useValue: mockHttp },
        { provide: ArtifactShareService, useValue: mockShares },
      ],
    });
  });

  afterEach(() => {
    // isolate:false shares one DOM across every spec file, so a fixture left
    // mounted keeps its placeholder mounted for the rest of the suite.
    fixture?.destroy();
    vi.restoreAllMocks();
  });

  // ----------------------------------------------------------------
  // Isolation — the invariants that make framing user HTML safe
  // ----------------------------------------------------------------

  it('sandboxes the iframe without allow-same-origin', async () => {
    await create();
    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    const sandbox = iframe()!.getAttribute('sandbox');
    expect(sandbox).toBe('allow-scripts');
    // `allow-same-origin` alongside `allow-scripts` would hand
    // attacker-authored markup the artifact origin itself. The thumbnail
    // frames the same untrusted bytes as the full viewer and is bound by
    // the same rule.
    expect(sandbox).not.toContain('allow-same-origin');
    expect(iframe()!.getAttribute('referrerpolicy')).toBe('no-referrer');
  });

  it('hides the frame from the keyboard and the accessibility tree', async () => {
    await create();
    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    // Without these, twelve cards put twelve whole documents in the tab
    // order and a keyboard user tabs through artifact *contents* to reach
    // the next card. `inert` is also what makes `aria-hidden` legitimate:
    // aria-hidden over focusable content is a defect on its own.
    expect(frame()).not.toBeNull();
    expect(frame()!.getAttribute('aria-hidden')).toBe('true');
    // The picture must not swallow the click the card wrapper is listening
    // for either.
    expect(iframe()!.className).toContain('pointer-events-none');
  });

  // ----------------------------------------------------------------
  // Cost — every mount is a Lambda invocation, so mounting is the budget
  // ----------------------------------------------------------------

  it('does not mint until the card is scrolled into view', async () => {
    await create();

    expect(mockHttp.mintRenderToken).not.toHaveBeenCalled();
    expect(iframe()).toBeNull();

    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockHttp.mintRenderToken).toHaveBeenCalledTimes(1);
    expect(mockHttp.mintRenderToken).toHaveBeenCalledWith('a1', 2, 'sess-9');
    expect(iframe()).not.toBeNull();
  });

  it('mints once even if the card re-enters the viewport', async () => {
    await create();

    const fire = intersect!;
    fire();
    await fixture.whenStable();
    // The observer disconnects on the first hit, so a real one would never
    // fire again — but scrolling back to a card must not re-invoke the
    // render Lambda even if it did.
    fire();
    await fixture.whenStable();

    expect(mockHttp.mintRenderToken).toHaveBeenCalledTimes(1);
  });

  it('mints immediately where IntersectionObserver is unavailable', async () => {
    stubObservers(false);
    await create();
    await fixture.whenStable();
    fixture.detectChanges();

    // Costs more than lazy mounting; a preview that never arrives at all is
    // worse.
    expect(mockHttp.mintRenderToken).toHaveBeenCalledTimes(1);
    expect(iframe()).not.toBeNull();
  });

  // ----------------------------------------------------------------
  // Rendering
  // ----------------------------------------------------------------

  it('scales the virtual viewport down to the measured card width', async () => {
    await create();
    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    // 512px card / 1024px virtual viewport.
    expect(frame()!.style.transform).toBe('scale(0.5)');
    expect(frame()!.style.width).toBe('1024px');

    setHostWidth(256);
    fireResize();
    fixture.detectChanges();

    expect(frame()!.style.transform).toBe('scale(0.25)');
  });

  it('keeps the placeholder until the frame has painted', async () => {
    await create();
    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    // The frame is mounted but transparent — a half-painted artifact
    // flashing into place reads as a broken card.
    expect(frame()!.className).toContain('opacity-0');

    iframe()!.dispatchEvent(new Event('load'));
    fixture.detectChanges();

    expect(frame()!.className).not.toContain('opacity-0');
  });

  it('falls back to the type glyph when minting fails', async () => {
    mockHttp.mintRenderToken.mockRejectedValue(new Error('503'));
    await create({ ...OWNED, contentType: 'text/csv' });
    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    // No frame, no error message: a card without a picture, which is the
    // card this page shipped with. The real failure surfaces on open.
    expect(iframe()).toBeNull();
    expect(el().querySelector('ng-icon')).not.toBeNull();
    expect(el().textContent).not.toContain('Try again');
  });

  // ----------------------------------------------------------------
  // The two access paths
  // ----------------------------------------------------------------

  it('mints a received artifact through the share endpoint', async () => {
    // A recipient has no artifact id, and the owner endpoint builds its
    // key from the authenticated session — calling it would 404. The
    // two kinds are two different credentials for the same bytes.
    await create({
      kind: 'shared',
      shareId: 'share-7',
      contentType: 'text/html',
    });
    intersect!();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockShares.mintSharedRenderToken).toHaveBeenCalledWith('share-7');
    expect(mockHttp.mintRenderToken).not.toHaveBeenCalled();
    expect(iframe()).not.toBeNull();
  });

  it('mints an owned artifact through the owner endpoint', async () => {
    await create();
    intersect!();
    await fixture.whenStable();

    expect(mockHttp.mintRenderToken).toHaveBeenCalledWith('a1', 2, 'sess-9');
    expect(mockShares.mintSharedRenderToken).not.toHaveBeenCalled();
  });
});
