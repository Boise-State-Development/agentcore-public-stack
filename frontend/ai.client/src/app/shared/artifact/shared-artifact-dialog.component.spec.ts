import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import { SharedArtifactDialogComponent } from './shared-artifact-dialog.component';
import { ShareService } from '../../session/services/share/share.service';

describe('SharedArtifactDialogComponent', () => {
  let fixture: ComponentFixture<SharedArtifactDialogComponent>;
  let mockShares: { mintConversationArtifactToken: ReturnType<typeof vi.fn> };
  let mockRef: { close: ReturnType<typeof vi.fn> };

  const el = () => fixture.nativeElement as HTMLElement;
  const iframe = () => el().querySelector('iframe');

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(SharedArtifactDialogComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockShares = {
      mintConversationArtifactToken: vi.fn().mockResolvedValue({
        url: 'https://artifacts.example/?t=jwt',
        expiresAt: '2026-09-05T00:02:00+00:00',
      }),
    };
    mockRef = { close: vi.fn() };

    TestBed.configureTestingModule({
      imports: [SharedArtifactDialogComponent],
      providers: [
        { provide: ShareService, useValue: mockShares },
        { provide: DialogRef, useValue: mockRef },
        {
          provide: DIALOG_DATA,
          useValue: {
            shareId: 'conv-share-1',
            artifact: {
              artifactId: 'art-1',
              version: 2,
              title: 'Quarterly Deck',
              contentType: 'text/html; charset=utf-8',
              producedByMessageIndex: 2,
            },
          },
        },
      ],
    });
  });

  afterEach(() => {
    // isolate:false shares one DOM, so a fixture left mounted keeps its
    // loading skeleton animating for the rest of the suite.
    fixture?.destroy();
    vi.restoreAllMocks();
  });

  it('mints through the conversation share, not an artifact share', async () => {
    await render();

    expect(mockShares.mintConversationArtifactToken).toHaveBeenCalledWith(
      'conv-share-1',
      'art-1',
    );
    expect(iframe()).not.toBeNull();
  });

  it('sandboxes the iframe without allow-same-origin', async () => {
    await render();

    // Same isolation the owner panel and the standalone recipient page
    // rely on — a third mint path must not weaken it.
    const sandbox = iframe()!.getAttribute('sandbox');
    expect(sandbox).toBe('allow-scripts');
    expect(sandbox).not.toContain('allow-same-origin');
    expect(iframe()!.getAttribute('referrerpolicy')).toBe('no-referrer');
  });

  it('gives one message for a refused and a missing artifact alike', async () => {
    mockShares.mintConversationArtifactToken.mockRejectedValue(
      new Error('404'),
    );
    await render();

    // "Not in this share" and "you may not open this share" are the same
    // fact to a recipient; telling them apart would describe what the
    // owner has.
    expect(el().textContent).toContain("couldn't be loaded");
    expect(iframe()).toBeNull();
  });

  it('offers no owner actions', async () => {
    await render();

    const text = el().textContent ?? '';
    expect(text).not.toContain('Download');
    expect(text).not.toContain('Rename');
    expect(text).not.toContain('Delete');
    // No source toggle either: the content endpoint is keyed on an
    // artifact share id, which a conversation share does not have, so
    // the control is absent rather than permanently failing.
    expect(text).not.toContain('Source');
    expect(text).toContain('Shared read-only');
  });

  it('closes on the close button', async () => {
    await render();
    const close = [...el().querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Close'),
    )!;
    close.click();

    expect(mockRef.close).toHaveBeenCalled();
  });

  it('drops a mint that resolves after a retry', async () => {
    let resolveFirst: (v: unknown) => void = () => undefined;
    mockShares.mintConversationArtifactToken
      .mockImplementationOnce(
        () => new Promise((r) => (resolveFirst = r)),
      )
      .mockResolvedValueOnce({
        url: 'https://artifacts.example/?t=second',
        expiresAt: '',
      });

    fixture = TestBed.createComponent(SharedArtifactDialogComponent);
    fixture.detectChanges();

    // Retry supersedes the in-flight first mint.
    (fixture.componentInstance as unknown as { retry: () => void }).retry();
    await fixture.whenStable();

    resolveFirst({ url: 'https://artifacts.example/?t=stale', expiresAt: '' });
    await fixture.whenStable();
    fixture.detectChanges();

    // The stale response must not overwrite the newer URL.
    expect(iframe()!.getAttribute('src')).toContain('t=second');
  });
});
