import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { SharedArtifactViewPage } from './shared-artifact-view.page';
import {
  ArtifactShareService,
  type SharedArtifact,
} from '../../session/services/artifacts/artifact-share.service';
import { ArtifactDownloadService } from '../../session/services/artifacts/artifact-download.service';

const SHARE_ID = 'share-1';

const META: SharedArtifact = {
  shareId: SHARE_ID,
  title: 'Quarterly Chart',
  contentType: 'text/html; charset=utf-8',
  version: 3,
  createdAt: '2026-09-03T00:00:00+00:00',
  ownerEmail: 'owner@example.com',
  canDownload: true,
};

describe('SharedArtifactViewPage', () => {
  let fixture: ComponentFixture<SharedArtifactViewPage>;
  let component: SharedArtifactViewPage;
  let shares: {
    getSharedArtifact: ReturnType<typeof vi.fn>;
    mintSharedRenderToken: ReturnType<typeof vi.fn>;
    getSharedArtifactContent: ReturnType<typeof vi.fn>;
  };
  let download: { download: ReturnType<typeof vi.fn> };

  let originalClipboard: PropertyDescriptor | undefined;

  const api = () => component as unknown as Record<string, any>;
  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';

  function build(shareId: string | null = SHARE_ID): void {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [SharedArtifactViewPage],
      providers: [
        // The banner's home link is a real routerLink, so the template
        // needs a router — a bare Router mock breaks rendering.
        provideRouter([]),
        // DI token overrides, not vi.mock — module mocks leak across specs.
        { provide: ArtifactShareService, useValue: shares },
        { provide: ArtifactDownloadService, useValue: download },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => shareId } } },
        },
      ],
    });
    fixture = TestBed.createComponent(SharedArtifactViewPage);
    component = fixture.componentInstance;
  }

  beforeEach(() => {
    shares = {
      getSharedArtifact: vi.fn().mockResolvedValue(META),
      mintSharedRenderToken: vi
        .fn()
        .mockResolvedValue({ url: 'https://artifacts.x/?t=jwt', expiresAt: 'e' }),
      getSharedArtifactContent: vi.fn().mockResolvedValue({
        content: '<h1>hi</h1>',
        contentType: 'text/html',
        version: 3,
      }),
    };
    download = { download: vi.fn().mockResolvedValue(true) };
    build();
    stubClipboard();
  });
  /**
   * `navigator.clipboard` doesn't exist in jsdom, so it has to be
   * defined rather than stubbed — and `Object.defineProperty` is NOT
   * undone by the `vi.unstubAllGlobals()` backstop in test-setup.ts.
   * The builder runs vitest with `isolate: false`, so a leaked global
   * here would follow the worker into unrelated spec files and surface
   * as one randomly-chosen file timing out. Restore it explicitly.
   */
  function stubClipboard(writeText = vi.fn().mockResolvedValue(undefined)) {
    originalClipboard = Object.getOwnPropertyDescriptor(
      navigator,
      'clipboard',
    );
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
  }

  function restoreClipboard(): void {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard);
    } else {
      delete (navigator as unknown as Record<string, unknown>)['clipboard'];
    }
    originalClipboard = undefined;
  }

  afterEach(() => {
    // isolate:false shares one DOM across every spec file, so a fixture
    // left mounted keeps its skeleton's infinite shimmer animation
    // running for the rest of the suite. Tear it down.
    fixture?.destroy();
    restoreClipboard();
  });

  /** Flush the promise chain the page's async init walks. */
  async function flush(): Promise<void> {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  }

  /**
   * `detectChanges()` runs `ngOnInit` itself, so calling it manually as
   * well would mint twice and make every call-count assertion lie.
   */
  async function init(): Promise<void> {
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();
  }

  // ----------------------------------------------------------------
  // Happy path
  // ----------------------------------------------------------------

  it('loads metadata, then mints — in that order', async () => {
    await init();
    // Metadata is the access check, so a denied viewer must never reach
    // the mint at all.
    expect(shares.getSharedArtifact).toHaveBeenCalledWith(SHARE_ID);
    expect(shares.mintSharedRenderToken).toHaveBeenCalledWith(SHARE_ID);
    expect(api()['safeUrl']()).not.toBeNull();
  });

  it('shows the read-only banner, title, version and owner', async () => {
    await init();
    expect(text()).toContain('Shared read-only snapshot');
    expect(text()).toContain('Quarterly Chart');
    expect(text()).toContain('Version 3');
    expect(text()).toContain('owner@example.com');
  });

  it('never asks for an artifact id — the share id is the only handle', async () => {
    await init();
    const allArgs = [
      ...shares.getSharedArtifact.mock.calls,
      ...shares.mintSharedRenderToken.mock.calls,
    ].flat();
    expect(allArgs).toEqual([SHARE_ID, SHARE_ID]);
  });

  it('offers no share or version-switching affordance', async () => {
    await init();
    const labels = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).map((b) => b.getAttribute('aria-label') ?? '');
    // A recipient view is read-only: no re-sharing, no moving between
    // versions (a share pins one immutable version).
    expect(labels.some((l) => /share/i.test(l))).toBe(false);
    expect(labels.some((l) => /change version/i.test(l))).toBe(false);
  });

  // ----------------------------------------------------------------
  // Error branches
  // ----------------------------------------------------------------

  it.each([
    [403, 'Access denied', "don't have permission"],
    [404, 'Artifact not found', 'may have been revoked'],
    [500, 'Something went wrong', 'Failed to load'],
  ])('renders the %i branch', async (status, heading, detail) => {
    shares.getSharedArtifact.mockRejectedValue({ status });
    await init();

    expect(text()).toContain(heading);
    expect(text()).toContain(detail);
    // Denied or missing means no credential is ever minted.
    expect(shares.mintSharedRenderToken).not.toHaveBeenCalled();
  });

  it('treats a missing route param as a 404 without calling the API', async () => {
    build(null);
    await init();
    expect(text()).toContain('Artifact not found');
    expect(shares.getSharedArtifact).not.toHaveBeenCalled();
  });

  it('falls back to 500 for a failure that never reached the server', async () => {
    shares.getSharedArtifact.mockRejectedValue(new Error('offline'));
    await init();
    expect(text()).toContain('Something went wrong');
  });

  it('turns a revoke-while-open into the dead-link page, not a retry box', async () => {
    shares.mintSharedRenderToken.mockRejectedValue({ status: 404 });
    await init();

    // Otherwise the viewer would show a "Try again" that can never work.
    expect(text()).toContain('Artifact not found');
    expect(api()['renderError']()).toBeNull();
  });

  it('shows a retryable message for a transient mint failure', async () => {
    shares.mintSharedRenderToken.mockRejectedValue({ status: 503 });
    await init();

    expect(api()['renderError']()).toContain("couldn't be loaded");
    // Still a live share — the page stays, only the viewer shows an error.
    expect(text()).toContain('Quarterly Chart');
  });

  it('re-mints on retry rather than reusing the expired token', async () => {
    await init();
    api()['retry']();
    await flush();
    // The token is a ~120s bearer credential; retry must mint a fresh one.
    expect(shares.mintSharedRenderToken).toHaveBeenCalledTimes(2);
  });

  // ----------------------------------------------------------------
  // Code view
  // ----------------------------------------------------------------

  it('fetches the source only when code view is opened', async () => {
    await init();
    expect(shares.getSharedArtifactContent).not.toHaveBeenCalled();

    api()['setView']('code');
    await flush();
    expect(shares.getSharedArtifactContent).toHaveBeenCalledWith(SHARE_ID);
  });

  it('does not refetch the source on a second switch to code', async () => {
    await init();
    api()['setView']('code');
    await flush();
    api()['setView']('preview');
    api()['setView']('code');
    await flush();

    expect(shares.getSharedArtifactContent).toHaveBeenCalledTimes(1);
  });

  it('steers an oversized artifact to download', async () => {
    shares.getSharedArtifactContent.mockRejectedValue(
      new HttpErrorResponse({ status: 413 }),
    );
    await init();
    api()['setView']('code');
    await flush();

    expect(api()['sourceError']()).toContain('too large');
  });

  it('reports a generic source failure for anything else', async () => {
    shares.getSharedArtifactContent.mockRejectedValue({ status: 500 });
    await init();
    api()['setView']('code');
    await flush();

    expect(api()['sourceError']()).toContain("couldn't be loaded");
  });

  it('copies the source to the clipboard', async () => {
    await init();
    api()['setView']('code');
    await flush();
    await api()['copy']();

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('<h1>hi</h1>');
    expect(api()['copied']()).toBe(true);
  });

  // ----------------------------------------------------------------
  // Download
  // ----------------------------------------------------------------

  it('downloads by share id, never by artifact id', async () => {
    await init();
    await api()['download']();

    // A recipient holds no artifact id they may mint against; the share
    // id is the authority and routes through the access-checked endpoint.
    expect(download.download).toHaveBeenCalledWith({ shareId: SHARE_ID });
  });

  it('hides the download button when the share disallows it', async () => {
    shares.getSharedArtifact.mockResolvedValue({ ...META, canDownload: false });
    await init();

    const labels = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).map((b) => b.getAttribute('aria-label') ?? '');
    expect(labels.some((l) => /download/i.test(l))).toBe(false);
  });

  it('offers a way back into the app', async () => {
    await init();
    // The route strips the shell chrome, so there is no sidenav to
    // navigate from — without this a recipient is stranded on the page.
    const home = (fixture.nativeElement as HTMLElement).querySelector('a[href="/"]');
    expect(home).not.toBeNull();
    expect(home!.textContent).toContain('boisestate.ai');
  });
});
