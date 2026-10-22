// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { Subject } from 'rxjs';
import { FEATURES } from '../../services/features';
import { SearchDialogService } from './search-dialog.service';

function setup(conversationSearch: boolean) {
  const closed = new Subject<unknown>();
  const ref = { closed, close: vi.fn(() => closed.next(undefined)) };
  const dialog = { open: vi.fn(() => ref), openDialogs: [] };
  TestBed.configureTestingModule({
    providers: [
      { provide: Dialog, useValue: dialog },
      { provide: FEATURES, useValue: { projects: false, conversationSearch } },
    ],
  });
  return { service: TestBed.inject(SearchDialogService), dialog, ref, closed };
}

describe('SearchDialogService', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('does nothing with the flag off', async () => {
    const { service, dialog } = setup(false);
    await service.open('q');
    expect(dialog.open).not.toHaveBeenCalled();
    expect(service.isOpen()).toBe(false);
  });

  it('opens one dialog with the hand-off query', async () => {
    const { service, dialog } = setup(true);
    await service.open('budget');
    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect((dialog.open.mock.calls[0] as unknown[])[1]).toMatchObject({ data: { query: 'budget' } });
    expect(service.isOpen()).toBe(true);
  });

  it('a second open refocuses instead of stacking, and carries a new query', async () => {
    const { service, dialog } = setup(true);
    await service.open();
    await service.open();
    await service.open('again');
    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect(service.refocus()).toEqual({ query: 'again', seq: 2 });
  });

  it('two opens racing the lazy import still open one dialog', async () => {
    const { service, dialog } = setup(true);
    await Promise.all([service.open(), service.open()]);
    expect(dialog.open).toHaveBeenCalledTimes(1);
  });

  it('can open again once closed', async () => {
    const { service, dialog, ref } = setup(true);
    await service.open();
    ref.close();
    expect(service.isOpen()).toBe(false);
    await service.open();
    expect(dialog.open).toHaveBeenCalledTimes(2);
  });
});
