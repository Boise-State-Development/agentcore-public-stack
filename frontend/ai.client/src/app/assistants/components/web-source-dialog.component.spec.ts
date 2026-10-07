import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { ToastService } from '../../services/toast/toast.service';
import { Document } from '../models/document.model';
import { WebSourceService } from '../services/web-source.service';
import { WebSourceDialogComponent, WebSourceDialogData } from './web-source-dialog.component';

describe('WebSourceDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: WebSourceService, useValue: { startCrawl: vi.fn() } },
        { provide: ToastService, useValue: { error: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<WebSourceDialogComponent, WebSourceDialogData, Document[]>(WebSourceDialogComponent, {
      data: { assistantId: 'AST-1' },
    });
  }

  it('names the dialog from its title and starts on the URL field', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Add web content' });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('web-src-url');
  });

  it('closes with no result on Escape', async () => {
    const { ref, container } = await open();
    let closed = false;
    let result: Document[] | undefined = [];
    ref.closed.subscribe((r) => {
      closed = true;
      result = r;
    });
    container
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(closed).toBe(true);
    expect(result).toBeUndefined();
  });
});
