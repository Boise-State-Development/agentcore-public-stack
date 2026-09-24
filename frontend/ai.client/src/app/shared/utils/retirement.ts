/**
 * Retirement: the picker rule and the copy shared by tool and model retirement
 * (docs/specs/mcp-server-retirement.md §7, docs/specs/model-retirement.md §7).
 *
 * The rule is the same for both: a non-`active` item can be turned **off** but
 * not **on**. Existing selections are untouched — this is a picker rule, never
 * an access decision.
 */

/**
 * An administrator has marked this item non-`active`: it must not be newly
 * selected. Absent/unknown `status` reads as active, so an older backend leaves
 * every picker exactly as it was.
 */
export function isRetiring(item: { status?: string | null }): boolean {
  return typeof item.status === 'string' && item.status !== 'active';
}

/** The retirement facts a picker needs, however the surface happens to carry them. */
export interface RetirementInfo {
  retirementNote?: string | null;
  retiresOn?: string | null;
}

/**
 * Render an ISO `retiresOn` for a human, or `null` if it is absent or unparseable.
 *
 * Parsed as UTC noon rather than `new Date('2026-10-31')`, which is midnight UTC
 * and prints as the day *before* for anyone west of Greenwich — which is
 * everyone here. A retirement date that reads a day early is the one kind of
 * wrong that would actually cost someone.
 */
export function formatRetiresOn(retiresOn?: string | null): string | null {
  if (!retiresOn) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(retiresOn);
  if (!m) return null;
  const [y, mo, day] = [+m[1], +m[2], +m[3]];
  const d = new Date(Date.UTC(y, mo - 1, day, 12));
  if (Number.isNaN(d.getTime())) return null;
  // `Date.UTC` ROLLS OVER rather than failing: 2026-13-45 becomes February 2027,
  // which would render as a confident, wrong retirement date. The backend
  // validator rejects that shape, but an older row or a hand-edited DynamoDB
  // item can still carry it, so re-read the parts and insist they match.
  if (d.getUTCFullYear() !== y || d.getUTCMonth() !== mo - 1 || d.getUTCDate() !== day) {
    return null;
  }
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
}

/** Admins punctuate or don't; strip a trailing stop so we never render "X..". */
function noteSentence(note?: string | null): string | null {
  return note?.trim().replace(/[.\s]+$/, '') || null;
}

/**
 * The one sentence every tool retirement surface appends after its own lead-in.
 *
 * Four shapes, because both fields are independently optional and the sentence
 * has to stay grammatical in all of them — this exists so the Customize card,
 * the detail page, the Designer notice and the schedule form cannot drift into
 * four different phrasings of the same fact.
 *
 * Deliberately says nothing when both are absent: an admin who set neither has
 * told us nothing, and "no replacement is available" is a claim we would be
 * inventing on their behalf.
 */
export function retirementDetail(info: RetirementInfo): string {
  const when = formatRetiresOn(info.retiresOn);
  const note = noteSentence(info.retirementNote);
  if (note && when) return `${note}. It stops working on ${when}.`;
  if (note) return `${note}.`;
  if (when) return `It stops working on ${when}.`;
  return '';
}

/** A model's retirement facts, plus the successor's display name when it has one. */
export interface ModelRetirementInfo extends RetirementInfo {
  status?: string | null;
  /** Display name of the `replacedBy` model, resolved by the caller. */
  successorName?: string | null;
}

/**
 * The model counterpart of `retirementDetail`. A model differs from a tool in
 * one way that changes the sentence: it usually has a successor that runs in
 * its place, so "it stops working" is only true when there is none.
 *
 * Says nothing when nothing is recorded, for the same reason as the tool copy.
 */
export function modelRetirementDetail(info: ModelRetirementInfo): string {
  const when = formatRetiresOn(info.retiresOn);
  const note = noteSentence(info.retirementNote);
  const successor = info.successorName?.trim() || null;
  let fact = '';
  if (info.status === 'retired') {
    fact = successor ? `${successor} now answers in its place.` : '';
  } else if (successor && when) {
    fact = `From ${when}, ${successor} answers in its place.`;
  } else if (successor) {
    fact = `${successor} will answer in its place.`;
  } else if (when) {
    fact = `It stops working on ${when}.`;
  }
  return [note ? `${note}.` : '', fact].filter(Boolean).join(' ');
}
