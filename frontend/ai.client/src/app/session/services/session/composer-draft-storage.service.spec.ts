import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  ComposerDraftStorageService,
  NEW_CONVERSATION_DRAFT_KEY,
  StoredAttachment,
  StoredComposerDraft,
  isEmptyDraft,
} from './composer-draft-storage.service';

const DAY_MS = 24 * 60 * 60 * 1000;

function draft(partial: Partial<StoredComposerDraft>): StoredComposerDraft {
  return { text: '', ...partial };
}

function attachment(id: string): StoredAttachment {
  return { uploadId: id, filename: `${id}.pdf`, mimeType: 'application/pdf', sizeBytes: 1234 };
}

function make(): ComposerDraftStorageService {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({});
  return TestBed.inject(ComposerDraftStorageService);
}

describe('ComposerDraftStorageService', () => {
  let service: ComposerDraftStorageService;

  beforeEach(() => {
    localStorage.clear();
    service = make();
  });

  afterEach(() => {
    vi.useRealTimers();
    localStorage.clear();
  });

  it('gives a conversation back its own draft and nobody else theirs', () => {
    service.write('s1', draft({ text: 'half a question' }));
    service.write('s2', draft({ text: 'a different half' }));

    expect(service.read('s1').text).toBe('half a question');
    expect(service.read('s2').text).toBe('a different half');
    expect(service.read('s3').text).toBe('');
  });

  it('survives a reload — a fresh instance reads what the last one wrote', () => {
    service.write(NEW_CONVERSATION_DRAFT_KEY, draft({ text: 'typed before the reload' }));

    expect(make().read(NEW_CONVERSATION_DRAFT_KEY).text).toBe('typed before the reload');
  });

  it('round-trips the mention, the queue and the attachments', () => {
    service.write(
      's1',
      draft({
        text: 'and one more thing',
        mentionAgentId: 'ast-42',
        queued: ['first follow-up', 'second follow-up'],
        attachments: [attachment('u1'), attachment('u2')],
        attachmentSessionId: 'staged-7',
      }),
    );

    const restored = make().read('s1');
    expect(restored.text).toBe('and one more thing');
    expect(restored.mentionAgentId).toBe('ast-42');
    expect(restored.queued).toEqual(['first follow-up', 'second follow-up']);
    expect(restored.attachments?.map(a => a.uploadId)).toEqual(['u1', 'u2']);
    expect(restored.attachmentSessionId).toBe('staged-7');
  });

  it('keeps a draft that is only a queued follow-up, or only an attachment', () => {
    service.write('s1', draft({ queued: ['typed while it was streaming'] }));
    service.write('s2', draft({ attachments: [attachment('u1')] }));

    expect(service.read('s1').queued).toEqual(['typed while it was streaming']);
    expect(service.read('s2').attachments).toHaveLength(1);
  });

  it('forgets a draft the composer emptied', () => {
    service.write('s1', draft({ text: 'about to be sent' }));
    service.write('s1', draft({ text: '' }));

    expect(service.read('s1').text).toBe('');
    expect(localStorage.getItem('composer-draft:s1')).toBeNull();
  });

  it('treats whitespace as empty rather than storing a blank draft', () => {
    service.write('s1', draft({ text: '   \n  ' }));

    expect(localStorage.getItem('composer-draft:s1')).toBeNull();
  });

  it('does not count a lone mention as something worth coming back to', () => {
    expect(isEmptyDraft(draft({ mentionAgentId: 'ast-42' }))).toBe(true);

    service.write('s1', draft({ mentionAgentId: 'ast-42' }));
    expect(localStorage.getItem('composer-draft:s1')).toBeNull();
  });

  it('drops an oversized paste but keeps the attachments under it', () => {
    service.write(
      's1',
      draft({ text: 'x'.repeat(40_000), attachments: [attachment('u1')] }),
    );

    const restored = service.read('s1');
    expect(restored.text).toBe('');
    expect(restored.attachments?.map(a => a.uploadId)).toEqual(['u1']);
  });

  it('expires a draft the user has not come back to in a month', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    service.write('s1', draft({ text: 'stale' }));

    vi.setSystemTime(new Date('2026-01-01T00:00:00Z').getTime() + 31 * DAY_MS);
    expect(service.read('s1').text).toBe('');
    expect(localStorage.getItem('composer-draft:s1')).toBeNull();
  });

  it('keeps a draft that is merely old', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    service.write('s1', draft({ text: 'still wanted' }));

    vi.setSystemTime(new Date('2026-01-01T00:00:00Z').getTime() + 29 * DAY_MS);
    expect(service.read('s1').text).toBe('still wanted');
  });

  it('caps the set, evicting the least recently written', () => {
    vi.useFakeTimers();
    const start = new Date('2026-01-01T00:00:00Z').getTime();
    for (let i = 0; i < 55; i++) {
      vi.setSystemTime(start + i * 1000);
      service.write(`s${i}`, draft({ text: `draft ${i}` }));
    }

    expect(service.read('s0').text).toBe('');
    expect(service.read('s4').text).toBe('');
    expect(service.read('s54').text).toBe('draft 54');
    expect(localStorage.length).toBe(50);
  });

  it('discards drafts belonging to the previous account', () => {
    service.adopt('user-a');
    service.write('s1', draft({ text: 'private to a' }));

    service.adopt('user-b');

    expect(service.read('s1').text).toBe('');
  });

  it('keeps drafts when the same account signs back in', () => {
    service.adopt('user-a');
    service.write('s1', draft({ text: 'mine' }));

    service.adopt('user-a');

    expect(service.read('s1').text).toBe('mine');
  });

  it('clears everything on logout, including the owner stamp', () => {
    service.adopt('user-a');
    service.write('s1', draft({ text: 'mine' }));

    service.clear();

    expect(service.read('s1').text).toBe('');
    expect(localStorage.getItem('composer-draft-owner')).toBeNull();
  });

  it('leaves unrelated localStorage keys alone', () => {
    localStorage.setItem('agents-view-mode', 'grid');
    service.write('s1', draft({ text: 'mine' }));

    service.clear();

    expect(localStorage.getItem('agents-view-mode')).toBe('grid');
  });

  it('reads an unparseable entry as no draft instead of throwing', () => {
    localStorage.setItem('composer-draft:s1', 'not json');

    expect(() => service.read('s1')).not.toThrow();
    expect(service.read('s1').text).toBe('');
  });

  it('rejects a hand-edited entry whose fields are the wrong shape', () => {
    localStorage.setItem(
      'composer-draft:s1',
      JSON.stringify({
        text: 'looks fine',
        updatedAt: Date.now(),
        queued: ['ok', 42, null],
        attachments: [attachment('u1'), { uploadId: 'u2' }, 'nope'],
        mentionAgentId: { not: 'a string' },
      }),
    );

    const restored = service.read('s1');
    expect(restored.queued).toEqual(['ok']);
    expect(restored.attachments?.map(a => a.uploadId)).toEqual(['u1']);
    expect(restored.mentionAgentId).toBeUndefined();
  });

  it('survives a localStorage that refuses to write', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError');
    });

    expect(() => service.write('s1', draft({ text: 'lost, but quietly' }))).not.toThrow();
    setItem.mockRestore();
  });
});
