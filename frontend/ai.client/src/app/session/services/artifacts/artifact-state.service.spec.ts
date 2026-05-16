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

  it('keeps the highest version for the same id (update_artifact)', () => {
    svc.recordLive(ev({ version: 1 }));
    svc.recordLive(ev({ version: 3, updatedAt: '2026-05-15T12:05:00+00:00' }));
    svc.recordLive(ev({ version: 2 })); // stale/out-of-order — ignored
    expect(svc.get('art-1')?.version).toBe(3);
    expect(svc.artifacts().length).toBe(1);
  });

  it('orders artifacts newest update first', () => {
    svc.recordLive(ev({ artifactId: 'old', updatedAt: '2026-05-15T10:00:00+00:00' }));
    svc.recordLive(ev({ artifactId: 'new', updatedAt: '2026-05-15T12:00:00+00:00' }));
    expect(svc.artifacts().map((a) => a.artifactId)).toEqual(['new', 'old']);
  });

  it('hydration does not clobber a higher live version', () => {
    svc.recordLive(ev({ version: 5, updatedAt: '2026-05-15T12:10:00+00:00' }));
    const stale: Artifact = {
      artifactId: 'art-1',
      version: 2,
      title: 'Old',
      contentType: 'text/html; charset=utf-8',
      updatedAt: '2026-05-15T11:00:00+00:00',
    };
    svc.seedFromHydration([stale]);
    expect(svc.get('art-1')?.version).toBe(5);
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

  it('reset clears artifacts and open panel', () => {
    svc.recordLive(ev());
    svc.openArtifactPanel({ artifactId: 'art-1', version: 1, title: 'Doc' });
    svc.reset();
    expect(svc.hasArtifacts()).toBe(false);
    expect(svc.openArtifact()).toBeNull();
  });
});
