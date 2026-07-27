/**
 * "Who can actually open this?" — one sentence, in the one place both surfaces read it from.
 *
 * Publication and reachability are separate axes and the store only guards one: the browse
 * read applies no access check, while the detail read enforces one. So an approved PRIVATE
 * or SHARED Agent gets a shelf tile that 404s for everyone it is not shared with.
 *
 * The author (at submit) and the reviewer (at approve) are the two people who can act on
 * that, and they must not be told two different things — this is the same reason
 * `runnabilityMessage` exists next door.
 *
 * ⚠️ Advisory, never a gate. Publishing a SHARED Agent to a team is a legitimate thing to
 * do; the wording says what will happen, and leaves the decision where it belongs.
 */

/** Mirrors the backend `ListingReachability`. */
export type ListingReachability = 'everyone' | 'shared_only' | 'owner_only';

/** Whether this reachability is worth interrupting anyone about. */
export function reachabilityIsLimited(value: ListingReachability): boolean {
  return value !== 'everyone';
}

/**
 * What the **author** is told before submitting — second person, and it names the fix.
 * Returns null when there is nothing to say.
 */
export function reachabilityAuthorMessage(value: ListingReachability): string | null {
  if (value === 'owner_only') {
    return (
      'Only you can open this agent. If it is published, people will see it in the store ' +
      'but get an error when they open it — set Visibility to Public first.'
    );
  }
  if (value === 'shared_only') {
    return (
      'Only people this agent is shared with can open it. Anyone else will see it in the ' +
      'store but get an error when they open it — set Visibility to Public to reach the ' +
      'whole university.'
    );
  }
  return null;
}

/**
 * What the **reviewer** is told before approving — third person, and it does not tell them
 * to go change someone else's access. Returns null when there is nothing to say.
 */
export function reachabilityReviewerMessage(value: ListingReachability): string | null {
  if (value === 'owner_only') {
    return "Private — only the author can open this. Approving it shelves a tile nobody else can use.";
  }
  if (value === 'shared_only') {
    return 'Shared — only people it is shared with can open it. Everyone else will get an error.';
  }
  return null;
}

/** Short label for a table cell, where the full sentence will not fit. */
export function reachabilityLabel(value: ListingReachability): string {
  if (value === 'owner_only') return 'Private';
  if (value === 'shared_only') return 'Shared';
  return 'Public';
}
