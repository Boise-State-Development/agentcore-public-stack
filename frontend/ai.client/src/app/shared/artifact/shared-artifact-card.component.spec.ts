import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';

import { SharedArtifactCardComponent } from './shared-artifact-card.component';
import { SharedArtifactDialogComponent } from './shared-artifact-dialog.component';
import type { SharedConversationArtifact } from '../../session/services/share/share.service';

function stubArtifact(
  overrides: Partial<SharedConversationArtifact> = {},
): SharedConversationArtifact {
  return {
    artifactId: 'art-1',
    version: 2,
    title: 'Quarterly Deck',
    contentType: 'text/html; charset=utf-8',
    producedByMessageIndex: 2,
    ...overrides,
  };
}

describe('SharedArtifactCardComponent', () => {
  let fixture: ComponentFixture<SharedArtifactCardComponent>;
  let mockDialog: { open: ReturnType<typeof vi.fn> };

  const el = () => fixture.nativeElement as HTMLElement;
  const button = () => el().querySelector('button')!;

  function render(
    artifact: SharedConversationArtifact = stubArtifact(),
    shareId = 'conv-share-1',
  ): void {
    fixture = TestBed.createComponent(SharedArtifactCardComponent);
    fixture.componentRef.setInput('artifact', artifact);
    fixture.componentRef.setInput('shareId', shareId);
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockDialog = { open: vi.fn() };
    TestBed.configureTestingModule({
      imports: [SharedArtifactCardComponent],
      providers: [{ provide: Dialog, useValue: mockDialog }],
    });
  });

  afterEach(() => {
    fixture?.destroy();
    vi.restoreAllMocks();
  });

  it('opens the read-only dialog with the conversation share as the grant', () => {
    render(stubArtifact({ artifactId: 'art-9' }), 'conv-share-7');
    button().click();

    expect(mockDialog.open).toHaveBeenCalledTimes(1);
    const [component, config] = mockDialog.open.mock.calls[0];
    expect(component).toBe(SharedArtifactDialogComponent);
    // There is no artifact share here — the pair (conversation shareId,
    // artifactId) is the whole handle a recipient has.
    expect(config.data.shareId).toBe('conv-share-7');
    expect(config.data.artifact.artifactId).toBe('art-9');
  });

  it('offers opening and nothing else', () => {
    render();

    // Download, share, rename and delete are all owner endpoints a
    // conversation-share recipient has no handle for. A visible control
    // that 403s is worse than an absent one.
    expect(el().querySelectorAll('button')).toHaveLength(1);
    expect(el().textContent).not.toContain('Download');
    expect(el().textContent).not.toContain('Delete');
    expect(el().textContent).not.toContain('Share');
  });

  it('names the artifact for screen readers', () => {
    render(stubArtifact({ title: 'Budget model', version: 3 }));
    const label = button().getAttribute('aria-label')!;
    expect(label).toContain('Budget model');
    expect(label).toContain('version 3');
  });

  it('falls back to a placeholder title', () => {
    render(stubArtifact({ title: '' }));
    expect(el().textContent).toContain('Untitled artifact');
  });

  it('labels the type from the content type, charset and all', () => {
    render(stubArtifact({ contentType: 'text/markdown' }));
    expect(el().textContent).toContain('Markdown');

    // The writer stores HTML with a charset suffix; a lookup keyed on
    // the raw string would fall through to the generic label.
    render(stubArtifact({ contentType: 'text/html; charset=utf-8' }));
    expect(el().textContent).toContain('Web page');
  });

  it('shows the version only when there is more than one', () => {
    render(stubArtifact({ version: 1 }));
    expect(el().textContent).not.toContain('v1');

    render(stubArtifact({ version: 4 }));
    expect(el().textContent).toContain('v4');
  });
});
