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
 * ⚠️ This used to describe publishing a SHARED Agent to a team as legitimate. It is not:
 * the marketplace is public-only, sharing with named coworkers is a separate mechanism,
 * and the backend now refuses anything short of PUBLIC at submit and at approve.
 *
 * So this file is now **reviewer-facing only**. The author never sees these strings: the
 * submit dialog asks them to consent to going public and does it for them. What survives
 * here is the case no submit-time gate can catch — an Agent published as PUBLIC and
 * narrowed afterwards, which is what the reviewer and the admin table need to know about.
 */

/** Mirrors the backend `ListingReachability`. */
export type ListingReachability = 'everyone' | 'shared_only' | 'owner_only';

/** Whether this reachability is worth interrupting anyone about. */
export function reachabilityIsLimited(value: ListingReachability): boolean {
  return value !== 'everyone';
}

/*
 * There was a `reachabilityAuthorMessage` here. It told the author to go "set Visibility
 * to Public first" — advice that is now wrong, because the submit dialog widens
 * visibility itself via its consent checkbox. The author-facing copy lives with that
 * control (`makePublicHelp`), where it can say what going public changes *from*.
 */

/**
 * What the **reviewer** is told before approving — third person, and it does not tell them
 * to go change someone else's access. Returns null when there is nothing to say.
 */
export function reachabilityReviewerMessage(value: ListingReachability): string | null {
  if (value === 'owner_only') {
    return 'Private — only the author can open this. Approving it publishes a tile nobody else can use.';
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
