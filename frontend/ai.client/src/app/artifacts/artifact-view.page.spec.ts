import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';

import { ArtifactViewPage } from './artifact-view.page';
import {
  ArtifactHttpService,
  type LibraryArtifact,
} from '../session/services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../session/services/artifacts/artifact-download.service';

function stubArtifact(overrides: Partial<LibraryArtifact> = {}): LibraryArtifact {
  return {
    artifactId: 'a1',
    version: 3,
    title: 'Quarterly plan',
    contentType: 'text/markdown',
    createdAt: '2026-05-01T09:00:00+00:00',
    updatedAt: '2026-05-15T10:00:00+00:00',
    sessionId: 'sess-1',
    ...overrides,
  };
}

describe('ArtifactViewPage', () => {
  let mockHttp: {
    listLibrary: ReturnType<typeof vi.fn>;
    mintRenderToken: ReturnType<typeof vi.fn>;
    getArtifactContent: ReturnType<typeof vi.fn>;
  };
  let mockDownload: { download: ReturnType<typeof vi.fn> };
  let paramId: string | null;

  beforeEach(() => {
    TestBed.resetTestingModule();
    paramId = 'a1';

    mockHttp = {
      listLibrary: vi.fn().mockResolvedValue([stubArtifact()]),
      mintRenderToken: vi
        .fn()
        .mockResolvedValue({ url: 'https://artifacts.example/a1?t=tok', expiresAt: '' }),
      getArtifactContent: vi.fn().mockResolvedValue({
        content: '# Hello',
        contentType: 'text/markdown',
        version: 3,
      }),
    };
    mockDownload = { download: vi.fn().mockResolvedValue(true) };

    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'artifacts', children: [] },
          { path: 's/:sessionId', children: [] },
        ]),
        { provide: ArtifactHttpService, useValue: mockHttp },
        { provide: ArtifactDownloadService, useValue: mockDownload },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => paramId } } },
        },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  async function createComponent() {
    const fixture = TestBed.createComponent(ArtifactViewPage);
    fixture.detectChanges(); // runs ngOnInit
    for (let i = 0; i < 6; i++) await Promise.resolve();
    fixture.detectChanges();
    return fixture;
  }

  function api(fixture: Awaited<ReturnType<typeof createComponent>>) {
    return fixture.componentInstance as unknown as {
      artifact: () => LibraryArtifact | null;
      isLoading: () => boolean;
      notFound: () => boolean;
      loadFailed: () => boolean;
      safeUrl: () => unknown;
      renderError: () => string | null;
      view: () => 'preview' | 'code';
      setView: (v: 'preview' | 'code') => void;
      source: () => unknown;
      sourceError: () => string | null;
      download: () => Promise<void>;
      openInNewTab: () => void;
      retry: () => void;
    };
  }

  it('renders the artifact without needing a pop-up', async () => {
    // The whole point of this page: a render token is minted and shown
    // in-app, so nothing a browser can refuse stands between the user and
    // their document.
    const openSpy = vi.spyOn(window, 'open');
    const c = api(await createComponent());

    expect(mockHttp.mintRenderToken).toHaveBeenCalledWith('a1', 3, 'sess-1');
    expect(c.safeUrl()).not.toBeNull();
    expect(c.renderError()).toBeNull();
    expect(openSpy).not.toHaveBeenCalled();
  });

  it('reports a missing artifact as not-found rather than a blank viewer', async () => {
    mockHttp.listLibrary.mockResolvedValue([stubArtifact({ artifactId: 'other' })]);
    const c = api(await createComponent());

    expect(c.notFound()).toBe(true);
    expect(mockHttp.mintRenderToken).not.toHaveBeenCalled();
  });

  it('distinguishes a failed lookup from a missing artifact', async () => {
    // "We couldn't reach the server" and "this does not exist" are
    // different problems and get different copy.
    mockHttp.listLibrary.mockRejectedValue(new Error('503'));
    const c = api(await createComponent());

    expect(c.loadFailed()).toBe(true);
    expect(c.notFound()).toBe(false);
  });

  it('surfaces a retryable error when minting fails', async () => {
    mockHttp.mintRenderToken.mockRejectedValue(new Error('500'));
    const c = api(await createComponent());

    expect(c.renderError()).toContain("couldn't be loaded");
    expect(c.safeUrl()).toBeNull();
  });

  it('fetches source only when the code view is opened, and only once', async () => {
    const c = api(await createComponent());
    expect(mockHttp.getArtifactContent).not.toHaveBeenCalled();

    c.setView('code');
    await Promise.resolve();
    await Promise.resolve();
    c.setView('preview');
    c.setView('code');
    await Promise.resolve();

    expect(mockHttp.getArtifactContent).toHaveBeenCalledTimes(1);
  });

  it('steers an oversized artifact to download instead of the code view', async () => {
    mockHttp.getArtifactContent.mockRejectedValue(
      new HttpErrorResponse({ status: 413 }),
    );
    const c = api(await createComponent());

    c.setView('code');
    for (let i = 0; i < 4; i++) await Promise.resolve();

    expect(c.sourceError()).toContain('too large');
  });

  it('downloads against the artifact id, the owner authority', async () => {
    // Recipients download by share id; an owner has the artifact itself.
    const c = api(await createComponent());
    await c.download();

    expect(mockDownload.download).toHaveBeenCalledWith({
      artifactId: 'a1',
      version: 3,
    });
  });

  it('treats a blocked pop-up on the new-tab action as a non-event', async () => {
    // Unlike the library's old primary action, a refusal here costs the
    // user nothing — the artifact is already rendered on this page — so it
    // must not throw or complain.
    vi.spyOn(window, 'open').mockImplementation(() => null);
    const c = api(await createComponent());

    expect(() => c.openInNewTab()).not.toThrow();
  });

  it('opens the new tab without noopener, and severs the opener itself', async () => {
    const tab = { location: { href: '' }, opener: {} as unknown, close: vi.fn() };
    const spy = vi
      .spyOn(window, 'open')
      .mockImplementation((_u?: string | URL, _t?: string, features?: string) =>
        /noopener|noreferrer/.test(features ?? '') ? null : (tab as unknown as Window),
      );

    const c = api(await createComponent());
    c.openInNewTab();

    expect(spy.mock.calls[0]?.[2] ?? '').not.toMatch(/noopener|noreferrer/);
    expect(tab.opener).toBeNull();
    expect(tab.location.href).toBe('https://artifacts.example/a1?t=tok');
  });

  it('re-mints on retry rather than reusing an expired token', async () => {
    // The token lives ~120s; a retry that replayed the old URL would load
    // an expired page and look like the retry failed.
    const c = api(await createComponent());
    expect(mockHttp.mintRenderToken).toHaveBeenCalledTimes(1);

    c.retry();
    await Promise.resolve();

    expect(mockHttp.mintRenderToken).toHaveBeenCalledTimes(2);
  });

  it('treats a missing route param as not-found', async () => {
    paramId = null;
    const c = api(await createComponent());

    expect(c.notFound()).toBe(true);
    expect(mockHttp.listLibrary).not.toHaveBeenCalled();
  });
});
