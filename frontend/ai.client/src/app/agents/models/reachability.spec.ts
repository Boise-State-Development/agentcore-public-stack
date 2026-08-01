import { describe, expect, it } from 'vitest';
import {
  ListingReachability,
  reachabilityIsLimited,
  reachabilityLabel,
  reachabilityReviewerMessage,
} from './reachability';

const LIMITED: ListingReachability[] = ['owner_only', 'shared_only'];

describe('reachability', () => {
  it('says nothing at all when the agent is public', () => {
    // The whole point is that this is quiet in the common case — a warning shown on every
    // row is a warning nobody reads.
    expect(reachabilityReviewerMessage('everyone')).toBeNull();
    expect(reachabilityIsLimited('everyone')).toBe(false);
  });

  it.each(LIMITED)('warns the reviewer when reachability is %s', (value) => {
    expect(reachabilityIsLimited(value)).toBe(true);
    expect(reachabilityReviewerMessage(value)).toBeTruthy();
  });

  it.each(LIMITED)('names the consequence, not just the state, for %s', (value) => {
    // A message that only says "this is private" tells the reader something they can
    // already see. Both voices have to say what will actually happen.
    expect(reachabilityReviewerMessage(value)!).toMatch(/nobody else can use|get an error/);
  });

  it('does not tell the reviewer to change someone else\u2019s visibility', () => {
    // The author owns visibility; the reviewer does not, and telling them to change it
    // would be inviting exactly the silent access-widening this feature refuses to do.
    // The author is no longer told anything here at all — the submit dialog's consent
    // checkbox widens visibility for them.
    expect(reachabilityReviewerMessage('owner_only')).not.toMatch(/set Visibility/i);
  });

  it('distinguishes owner_only from shared_only rather than collapsing them', () => {
    expect(reachabilityReviewerMessage('owner_only')).not.toBe(
      reachabilityReviewerMessage('shared_only'),
    );
  });

  it('labels each state for a table cell', () => {
    expect(reachabilityLabel('everyone')).toBe('Public');
    expect(reachabilityLabel('shared_only')).toBe('Shared');
    expect(reachabilityLabel('owner_only')).toBe('Private');
  });
});
