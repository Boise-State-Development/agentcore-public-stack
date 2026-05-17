import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ArtifactStateService } from './artifact-state.service';
import type { ArtifactEvent } from '../../../shared/utils/stream-parser';
import type { Artifact } from './artifact.model';

function ev(over: Partial<ArtifactEvent> = {}): ArtifactEvent {
  return {
    type: 'artifact',
    artifactId: 'art-1',
    version: 1,
    title: 'Doc',
    contentType: 'text/html; charset=utf-8',
    sessionId: 'sess-9',
    updatedAt: '2026-05-15T12:00:00+00:00',
    action: 'created',
    ...over,
  };
}

describe('ArtifactStateService', () => {
  let svc: ArtifactStateService;

  beforeEach(() => {
    // ArtifactStateService injects SidenavService (both providedIn:
    // 'root'), so it must be created in an injection context rather
    // than via `new`.
    TestBed.configureTestingModule({});
    svc = TestBed.inject(ArtifactStateService);
  });

  it('starts empty', () => {
    expect(svc.hasArtifacts()).toBe(false);
    expect(svc.artifacts()).toEqual([]);
    expect(svc.openArtifact()).toBeNull();
  });

  it('records a live artifact event', () => {
    svc.recordLive(ev());
    expect(svc.hasArtifacts()).toBe(true);
    expect(svc.get('art-1')?.version).toBe(1);
  });

  it('threads producedByMessageIndex from the live event', () => {
    svc.recordLive(ev({ producedByMessageIndex: 7 }));
    expect(svc.get('art-1')?.producedByMessageIndex).toBe(7);
  });

  it('defaults producedByMessageIndex to null when the event omits it', () => {
    svc.recordLive(ev());
    expect(svc.get('art-1')?.producedByMessageIndex).toBeNull();
  });

  it('stores the live producing message id when provided', () => {
    svc.recordLive(ev(), 'msg-sess-9-3');
    expect(svc.get('art-1')?.producedByMessageId).toBe('msg-sess-9-3');
  });

  it('defaults producedByMessageId to null on the hydration-style call', () => {
    svc.recordLive(ev());
    expect(svc.get('art-1')?.producedByMessageId).toBeNull();
  });

  it('keeps every version of an artifact as its own entry', () => {
    svc.recordLive(ev({ version: 1 }));
    svc.recordLive(ev({ version: 3, updatedAt: '2026-05-15T12:05:00+00:00' }));
    svc.recordLive(ev({ version: 2, updatedAt: '2026-05-15T12:02:00+00:00' }));
    // All three versions coexist (one card per version); get() resolves
    // the highest.
    expect(svc.artifacts().length).toBe(3);
    expect(
      svc
        .artifacts()
        .map((a) => a.version)
        .sort(),
    ).toEqual([1, 2, 3]);
    expect(svc.get('art-1')?.version).toBe(3);
  });

  it('orders versions of one artifact by version when timestamps tie', () => {
    const at = '2026-05-15T12:00:00+00:00';
    svc.recordLive(ev({ version: 1, updatedAt: at }));
    svc.recordLive(ev({ version: 2, updatedAt: at }));
    expect(svc.artifacts().map((a) => a.version)).toEqual([2, 1]);
  });

  it('versionsFor returns one artifact’s versions, newest first', () => {
    svc.recordLive(ev({ artifactId: 'a', version: 1 }));
    svc.recordLive(ev({ artifactId: 'a', version: 2 }));
    svc.recordLive(ev({ artifactId: 'a', version: 3 }));
    svc.recordLive(ev({ artifactId: 'b', version: 1 }));
    expect(svc.versionsFor('a').map((v) => v.version)).toEqual([3, 2, 1]);
    expect(svc.versionsFor('b').map((v) => v.version)).toEqual([1]);
    expect(svc.versionsFor('missing')).toEqual([]);
  });

  it('orders artifacts newest update first', () => {
    svc.recordLive(ev({ artifactId: 'old', updatedAt: '2026-05-15T10:00:00+00:00' }));
    svc.recordLive(ev({ artifactId: 'new', updatedAt: '2026-05-15T12:00:00+00:00' }));
    expect(svc.artifacts().map((a) => a.artifactId)).toEqual(['new', 'old']);
  });

  it('hydration adds older versions alongside a newer live one', () => {
    svc.recordLive(ev({ version: 5, updatedAt: '2026-05-15T12:10:00+00:00' }));
    const v2: Artifact = {
      artifactId: 'art-1',
      version: 2,
      title: 'Old',
      contentType: 'text/html; charset=utf-8',
      updatedAt: '2026-05-15T11:00:00+00:00',
    };
    svc.seedFromHydration([v2]);
    // Distinct versions coexist (history), get() still resolves latest.
    expect(svc.artifacts().map((a) => a.version).sort()).toEqual([2, 5]);
    expect(svc.get('art-1')?.version).toBe(5);
  });

  it('live anchor wins when hydration repeats the same (id, version)', () => {
    svc.recordLive(ev({ version: 1 }), 'msg-sess-9-3');
    svc.seedFromHydration([
      {
        artifactId: 'art-1',
        version: 1,
        title: 'Doc',
        contentType: 'text/html; charset=utf-8',
        updatedAt: '2026-05-15T12:00:00+00:00',
        producedByMessageIndex: 4,
      },
    ]);
    // The live entry's precise per-turn anchor must survive a later
    // index-only hydration of the same version.
    expect(svc.get('art-1')?.producedByMessageId).toBe('msg-sess-9-3');
  });

  it('a live event fills in the anchor a prior hydration row lacked', () => {
    svc.seedFromHydration([
      {
        artifactId: 'art-1',
        version: 1,
        title: 'Doc',
        contentType: 'text/html; charset=utf-8',
        updatedAt: '2026-05-15T12:00:00+00:00',
      },
    ]);
    svc.recordLive(ev({ version: 1 }), 'msg-sess-9-3');
    expect(svc.get('art-1')?.producedByMessageId).toBe('msg-sess-9-3');
  });

  it('hydration adds artifacts not seen live', () => {
    svc.seedFromHydration([
      {
        artifactId: 'h1',
        version: 2,
        title: 'Hydrated',
        contentType: 'text/html; charset=utf-8',
        updatedAt: '2026-05-15T09:00:00+00:00',
      },
    ]);
    expect(svc.get('h1')?.title).toBe('Hydrated');
  });

  it('opens and closes the panel', () => {
    svc.openArtifactPanel({ artifactId: 'art-1', version: 1, title: 'Doc' });
    expect(svc.openArtifact()).toEqual({ artifactId: 'art-1', version: 1, title: 'Doc' });
    svc.closeArtifactPanel();
    expect(svc.openArtifact()).toBeNull();
  });

  it('auto-opens the panel for a created artifact', () => {
    svc.recordLive(ev({ artifactId: 'art-9', version: 1, title: 'Generated' }));
    expect(svc.openArtifact()).toEqual({
      artifactId: 'art-9',
      version: 1,
      title: 'Generated',
    });
  });

  it('re-points the panel to the new version when an artifact is updated', () => {
    svc.recordLive(ev({ version: 1, action: 'created' }));
    svc.recordLive(
      ev({
        version: 2,
        action: 'updated',
        title: 'Doc v2',
        updatedAt: '2026-05-15T12:05:00+00:00',
      }),
    );
    expect(svc.get('art-1')?.version).toBe(2);
    expect(svc.openArtifact()).toEqual({
      artifactId: 'art-1',
      version: 2,
      title: 'Doc v2',
    });
  });

  it('re-opens the panel on an update even after the user closed it', () => {
    svc.recordLive(ev({ version: 1, action: 'created' }));
    svc.closeArtifactPanel();
    expect(svc.openArtifact()).toBeNull();
    svc.recordLive(
      ev({ version: 2, action: 'updated', updatedAt: '2026-05-15T12:05:00+00:00' }),
    );
    expect(svc.openArtifact()?.version).toBe(2);
  });

  it('keeps the panel on the highest version for an out-of-order update', () => {
    svc.recordLive(ev({ version: 3, updatedAt: '2026-05-15T12:10:00+00:00' }));
    svc.recordLive(ev({ version: 2, action: 'updated' })); // stale — ignored
    expect(svc.get('art-1')?.version).toBe(3);
    expect(svc.openArtifact()?.version).toBe(3);
  });

  it('hydration on reload does not auto-open the panel', () => {
    svc.seedFromHydration([
      {
        artifactId: 'h1',
        version: 1,
        title: 'Hydrated',
        contentType: 'text/html; charset=utf-8',
        updatedAt: '2026-05-15T09:00:00+00:00',
      },
    ]);
    expect(svc.openArtifact()).toBeNull();
  });

  it('reset clears artifacts and open panel', () => {
    svc.recordLive(ev());
    svc.openArtifactPanel({ artifactId: 'art-1', version: 1, title: 'Doc' });
    svc.reset();
    expect(svc.hasArtifacts()).toBe(false);
    expect(svc.openArtifact()).toBeNull();
  });
});
