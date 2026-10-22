import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { ComposerDraftService } from './composer-draft.service';

describe('ComposerDraftService', () => {
  let service: ComposerDraftService;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    service = TestBed.inject(ComposerDraftService);
  });

  it('hands a draft only to the composer of the session it was requested for', () => {
    service.request('s1', 'redo it');
    expect(service.consume('s2')).toBeNull();
    expect(service.pending()?.text).toBe('redo it');
    expect(service.consume('s1')?.text).toBe('redo it');
    expect(service.pending()).toBeNull();
    expect(service.consume('s1')).toBeNull();
  });

  it('two identical requests are distinct', () => {
    service.request('s1', 'x');
    const first = service.pending()!.nonce;
    service.request('s1', 'x');
    expect(service.pending()!.nonce).not.toBe(first);
  });
});
