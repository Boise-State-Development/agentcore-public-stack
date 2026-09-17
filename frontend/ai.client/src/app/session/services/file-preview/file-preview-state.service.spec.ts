import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { FilePreviewStateService } from './file-preview-state.service';
import { ArtifactStateService } from '../artifacts/artifact-state.service';
import { DockedPaneService } from '../docked-pane/docked-pane.service';

describe('FilePreviewStateService', () => {
  let service: FilePreviewStateService;
  let artifacts: ArtifactStateService;
  let dockedPane: DockedPaneService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(FilePreviewStateService);
    artifacts = TestBed.inject(ArtifactStateService);
    dockedPane = TestBed.inject(DockedPaneService);
  });

  const file = { uploadId: 'up1', filename: 'plan.docx' };

  it('starts closed', () => {
    expect(service.openFile()).toBeNull();
  });

  it('opens on a file and claims the rail', () => {
    service.open(file);

    expect(service.openFile()).toEqual(file);
    expect(dockedPane.owner()).toBe('file-preview');
  });

  it('closes and releases the rail', () => {
    service.open(file);
    service.close();

    expect(service.openFile()).toBeNull();
    expect(dockedPane.isOpen()).toBe(false);
  });

  it('reads as closed once an artifact takes the rail', () => {
    service.open(file);

    artifacts.openArtifactPanel({
      artifactId: 'art-1',
      version: 1,
      title: 'Report',
    });

    expect(service.openFile()).toBeNull();
    expect(artifacts.openArtifact()).not.toBeNull();
  });

  it('displaces an open artifact when a preview opens', () => {
    artifacts.openArtifactPanel({
      artifactId: 'art-1',
      version: 1,
      title: 'Report',
    });

    service.open(file);

    expect(artifacts.openArtifact()).toBeNull();
    expect(service.openFile()).toEqual(file);
  });

  it('does not reopen the displaced artifact when the preview closes', () => {
    // The rail is freed, not handed back: a user who closed the preview
    // asked for the chat, not for whatever was docked two actions ago.
    artifacts.openArtifactPanel({
      artifactId: 'art-1',
      version: 1,
      title: 'Report',
    });
    service.open(file);

    service.close();

    expect(artifacts.openArtifact()).toBeNull();
    expect(service.openFile()).toBeNull();
    expect(dockedPane.isOpen()).toBe(false);
  });

  it('reset closes an open preview', () => {
    service.open(file);
    service.reset();

    expect(service.openFile()).toBeNull();
    expect(dockedPane.isOpen()).toBe(false);
  });

  it('reset does not close an artifact that holds the rail', () => {
    // Session change resets both services; the preview's reset must not
    // reach past its own pane and shut the artifact's.
    artifacts.openArtifactPanel({
      artifactId: 'art-1',
      version: 1,
      title: 'Report',
    });

    service.reset();

    expect(artifacts.openArtifact()).not.toBeNull();
  });
});
