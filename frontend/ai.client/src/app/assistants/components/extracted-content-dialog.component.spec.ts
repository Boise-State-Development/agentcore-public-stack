import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { DocumentService } from '../services/document.service';
import { ExtractedChunksResponse } from '../models/document.model';
import {
  ExtractedContentDialogComponent,
  ExtractedContentDialogData,
} from './extracted-content-dialog.component';

describe('ExtractedContentDialogComponent', () => {
  let getExtractedChunks: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getExtractedChunks = vi.fn();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: DocumentService, useValue: { getExtractedChunks } }],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<ExtractedContentDialogComponent, ExtractedContentDialogData>(
      ExtractedContentDialogComponent,
      { data: { assistantId: 'a1', documentId: 'd1', filename: 'syllabus.pdf' } },
    );
  }

  it('names the dialog from its title and describes it with the filename', async () => {
    getExtractedChunks.mockResolvedValue({
      documentId: 'd1',
      fileName: 'syllabus.pdf',
      engine: 'managed',
      available: true,
      chunks: [{ text: 'Week 1', order: 0, page: 2 }],
      returned: 1,
      capReached: false,
    } satisfies ExtractedChunksResponse);
    const { container } = await open();
    expectNamedDialog(container, { name: 'Extracted content', description: 'syllabus.pdf' });
    expect(getExtractedChunks).toHaveBeenCalledWith('a1', 'd1');
  });

  it('explains inside the dialog when the content cannot be read', async () => {
    getExtractedChunks.mockRejectedValue(new Error('boom'));
    const { container } = await open();
    await new Promise((resolve) => setTimeout(resolve, 0));
    TestBed.tick();
    expect(container.textContent).toContain('Could not read the extracted content');
  });
});
