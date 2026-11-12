/**
 * The review layer of a `.docx` preview: who changed what, when, and what
 * they said about it.
 *
 * docx-preview 0.4.0 renders tracked changes as bare `<ins>` / `<del>`
 * (it keeps the date and drops the author), and no comment reaches the
 * page at all: its reference glyph is lost to a bug in the library's
 * element factory (see `decorateComments`), and the commented range is
 * registered as a CSS highlight the library never styles.
 *
 * Everything here works from what the library already parsed, plus one
 * read of `comments.xml` for the paragraph ids Word threads replies by,
 * which the library's model does not keep. Nothing is invented: a
 * document without comments or changes produces an empty review and is
 * rendered exactly as before.
 */

/** Where a tracked change's identity rides through the renderer.
 *
 * The library renders a change with `renderContainer(elem, 'ins',
 * { dateTime: elem.date })` and passes nothing else from the node, so the
 * date is the only channel from a model node to its element. Each change
 * node's date is swapped for this token before rendering and the real
 * date is put back on the element afterwards (`decorateRevisions`). The
 * token survives the renderer splitting a paragraph across pages, since
 * every piece is rendered from a node carrying it. */
const REVISION_TOKEN = 'docx-rev:';

/** Comment range markers the library emits as HTML comment nodes, read
 *  back to find each comment's place in the page. */
const RANGE_MARKER = /^(start|end) of comment #(.+)$/;

/** Number of distinct author colours (`data-author` 0..n-1). */
export const AUTHOR_COLOURS = 7;

export type RevisionKind = 'insert' | 'delete' | 'replace';

export interface DocxRevision {
  /** Stable within one render; `data-revision` on its elements. */
  readonly id: string;
  readonly kind: RevisionKind;
  readonly author: string;
  /** ISO date as written in the document, or null when absent. */
  readonly date: string | null;
  readonly inserted: string;
  readonly deleted: string;
}

export interface DocxComment {
  readonly id: string;
  readonly author: string;
  readonly initials: string;
  readonly date: string | null;
  readonly text: string;
}

/** A top-level comment and its replies, in the order Word lists them. */
export interface DocxCommentThread {
  readonly id: string;
  readonly comment: DocxComment;
  readonly replies: readonly DocxComment[];
  /** Marked resolved in Word ("Resolve thread"). */
  readonly resolved: boolean;
}

/** One row of the review list, in document order. */
export type DocxReviewItem =
  | {
      readonly kind: 'comment';
      readonly key: string;
      /** 1-based, matches the marker drawn in the page. */
      readonly number: number;
      readonly thread: DocxCommentThread;
    }
  | { readonly kind: 'revision'; readonly key: string; readonly revision: DocxRevision };

/** The slice of docx-preview's parsed model this module reads. */
export interface DocxModelNode {
  type?: string;
  children?: DocxModelNode[];
  text?: string;
  id?: string;
  author?: string;
  initials?: string;
  date?: string;
}

const textOf = (node: DocxModelNode, wantDeleted: boolean): string => {
  if (node.type === 'text' && !wantDeleted) return node.text ?? '';
  if (node.type === 'deletedText' && wantDeleted) return node.text ?? '';
  if (node.type === 'tab') return '\t';
  return (node.children ?? []).map((c) => textOf(c, wantDeleted)).join('');
};

/**
 * Give every tracked insertion and deletion in `body` an id that will
 * reach its rendered element, and list them in document order.
 *
 * A deletion followed directly by an insertion from the same author is
 * one replacement — that is what a reviewer did, and how our own
 * `redline_word_document` writes a `replace`. Mutates the model: call it
 * between `parseAsync` and `renderDocument`.
 */
export function tagRevisions(body: DocxModelNode | undefined): DocxRevision[] {
  const revisions: DocxRevision[] = [];
  let seq = 0;

  const visit = (node: DocxModelNode): void => {
    const children = node.children ?? [];
    // The deletion directly before the current child, if any — the half of
    // a replacement waiting for its insertion.
    let pendingDelete: DocxRevision | null = null;

    for (const child of children) {
      const isInsert = child.type === 'inserted';
      const isDelete = child.type === 'deleted';
      if (!isInsert && !isDelete) {
        pendingDelete = null;
        visit(child);
        continue;
      }

      const author = child.author?.trim() || 'Unknown author';
      const date = child.date || null;
      const text = textOf(child, isDelete);

      if (isInsert && pendingDelete && pendingDelete.author === author) {
        const merged: DocxRevision = { ...pendingDelete, kind: 'replace', inserted: text };
        revisions[revisions.length - 1] = merged;
        child.date = `${REVISION_TOKEN}${merged.id}|${date ?? ''}`;
        pendingDelete = null;
        continue;
      }

      const revision: DocxRevision = {
        id: String(++seq),
        kind: isInsert ? 'insert' : 'delete',
        author,
        date,
        inserted: isInsert ? text : '',
        deleted: isDelete ? text : '',
      };
      revisions.push(revision);
      child.date = `${REVISION_TOKEN}${revision.id}|${date ?? ''}`;
      pendingDelete = isDelete ? revision : null;
    }
  };

  if (body) visit(body);
  return revisions;
}

const attr = (el: Element, localName: string): string | null => {
  for (const a of Array.from(el.attributes)) {
    if (a.localName === localName) return a.value;
  }
  return null;
};

/** Comment id → the `w14:paraId` of its last paragraph, which is the key
 *  `commentsExtended.xml` uses for reply links and resolved state. */
export function commentParaIds(commentsXml: string | null): Map<string, string> {
  const ids = new Map<string, string>();
  if (!commentsXml || typeof DOMParser === 'undefined') return ids;
  const xml = new DOMParser().parseFromString(commentsXml, 'application/xml');
  for (const el of Array.from(xml.getElementsByTagNameNS('*', 'comment'))) {
    const id = attr(el, 'id');
    const paragraphs = Array.from(el.getElementsByTagNameNS('*', 'p'));
    const paraId = paragraphs.length ? attr(paragraphs[paragraphs.length - 1], 'paraId') : null;
    if (id && paraId) ids.set(id, paraId);
  }
  return ids;
}

export interface CommentExtension {
  paraIdParent?: string;
  done?: boolean;
}

/**
 * Group the document's comments into threads.
 *
 * Replies are linked through `commentsExtended.xml` (Word 2013+): a
 * reply's last paragraph names its parent's last paragraph. A document
 * without that part — older Word, LibreOffice, our own redline tool —
 * has no replies, so every comment is its own thread.
 */
export function threadComments(
  comments: readonly DocxModelNode[] | undefined,
  paraIds: ReadonlyMap<string, string>,
  extensions: Readonly<Record<string, CommentExtension>> | undefined,
): DocxCommentThread[] {
  const flat: DocxComment[] = (comments ?? []).map((c) => ({
    id: String(c.id ?? ''),
    author: c.author?.trim() || 'Unknown author',
    initials: c.initials?.trim() || '',
    date: c.date || null,
    text: (c.children ?? [])
      .map((p) => textOf(p, false))
      .join('\n')
      .trim(),
  }));

  const byParaId = new Map<string, DocxComment>();
  for (const c of flat) {
    const paraId = paraIds.get(c.id);
    if (paraId) byParaId.set(paraId, c);
  }
  const extensionOf = (c: DocxComment): CommentExtension | undefined => {
    const paraId = paraIds.get(c.id);
    return paraId ? extensions?.[paraId] : undefined;
  };

  // Walk to the root so a reply to a reply still lands in its thread. A
  // link that loops back is broken data; the comment stands alone rather
  // than appearing in two threads.
  const rootOf = (c: DocxComment): DocxComment => {
    let current = c;
    const seen = new Set<string>();
    for (;;) {
      seen.add(current.id);
      const parentPara = extensionOf(current)?.paraIdParent;
      const parent = parentPara ? byParaId.get(parentPara) : undefined;
      if (!parent) return current;
      if (seen.has(parent.id)) return c;
      current = parent;
    }
  };

  const threads = new Map<string, { comment: DocxComment; replies: DocxComment[] }>();
  for (const c of flat) {
    const root = rootOf(c);
    const thread = threads.get(root.id) ?? { comment: root, replies: [] };
    threads.set(root.id, thread);
    if (root !== c) thread.replies.push(c);
  }

  return Array.from(threads.values()).map(({ comment, replies }) => ({
    id: comment.id,
    comment,
    replies,
    resolved: !!extensionOf(comment)?.done,
  }));
}

/** Stable author → colour slot, in order of first appearance. */
export function authorIndex(
  revisions: readonly DocxRevision[],
  threads: readonly DocxCommentThread[],
): Map<string, number> {
  const index = new Map<string, number>();
  const add = (author: string): void => {
    if (!index.has(author)) index.set(author, index.size % AUTHOR_COLOURS);
  };
  revisions.forEach((r) => add(r.author));
  threads.forEach((t) => [t.comment, ...t.replies].forEach((c) => add(c.author)));
  return index;
}

const dateFormat =
  typeof Intl !== 'undefined'
    ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : null;

/** Locale date for display; null for a missing or unreadable date. */
export function formatReviewDate(date: string | null): string | null {
  if (!date) return null;
  const parsed = new Date(date);
  if (Number.isNaN(parsed.getTime())) return null;
  return dateFormat ? dateFormat.format(parsed) : parsed.toISOString();
}

const KIND_VERB: Record<RevisionKind, string> = {
  insert: 'Inserted',
  delete: 'Deleted',
  replace: 'Replaced',
};

export const revisionVerb = (kind: RevisionKind): string => KIND_VERB[kind];

export interface DecoratedReview {
  /** Review item key (`c:<thread>` / `r:<revision>`) → its element in the page. */
  readonly anchors: Map<string, Element>;
  /** Thread id → the text the comment covers. */
  readonly ranges: Map<string, Range>;
  /** Thread id → 1-based number, in page order; matches the marker. */
  readonly numbers: Map<string, number>;
}

/** Restore each change element's real date, and tag it with its revision
 *  id, its author's colour and a tooltip naming who and when. */
function decorateRevisions(
  root: HTMLElement,
  revisions: readonly DocxRevision[],
  authors: ReadonlyMap<string, number>,
  anchors: Map<string, Element>,
): void {
  const byId = new Map(revisions.map((r) => [r.id, r]));
  for (const el of Array.from(root.querySelectorAll('ins[datetime], del[datetime]'))) {
    const raw = el.getAttribute('datetime') ?? '';
    if (!raw.startsWith(REVISION_TOKEN)) continue;
    const [id, ...rest] = raw.slice(REVISION_TOKEN.length).split('|');
    const date = rest.join('|');
    if (date) el.setAttribute('datetime', date);
    else el.removeAttribute('datetime');

    const revision = byId.get(id);
    if (!revision) continue;
    el.setAttribute('data-revision', id);
    el.setAttribute('data-author', String(authors.get(revision.author) ?? 0));
    const when = formatReviewDate(revision.date);
    const verb = el.tagName === 'INS' ? 'Inserted' : 'Deleted';
    el.setAttribute('title', `${verb} by ${revision.author}${when ? ` · ${when}` : ''}`);
    if (!anchors.has(`r:${id}`)) anchors.set(`r:${id}`, el);
  }
}

/**
 * Put a marker at the end of each thread's commented range, and find the
 * range itself.
 *
 * The marker goes where Word puts the comment's reference run. The
 * library's own reference is unusable: it renders it as an `h()`
 * `#fragment`, and docx-preview 0.4.0's `h` returns an empty fragment
 * without appending children, so the 💬 glyph and its popover never reach
 * the page. Any that a later version does render are removed, so a
 * library upgrade cannot double the markers.
 *
 * A reply's range sits at its parent's; only the thread gets a marker, so
 * one conversation reads as one marker, the way Word draws it.
 */
function decorateComments(
  root: HTMLElement,
  threads: readonly DocxCommentThread[],
  authors: ReadonlyMap<string, number>,
  anchors: Map<string, Element>,
  ranges: Map<string, Range>,
): void {
  for (const el of Array.from(root.querySelectorAll('.docx-comment-ref, .docx-comment-popover'))) {
    el.remove();
  }

  const starts = new Map<string, Comment>();
  const ends = new Map<string, Comment>();
  // Collect first, mutate after: editing the tree mid-walk skips nodes.
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_COMMENT);
  while (walker.nextNode()) {
    const node = walker.currentNode as Comment;
    const marker = RANGE_MARKER.exec(node.data);
    if (marker) (marker[1] === 'start' ? starts : ends).set(marker[2], node);
  }

  for (const thread of threads) {
    const start = starts.get(thread.id);
    const end = ends.get(thread.id);
    if (start && end) {
      const range = root.ownerDocument.createRange();
      range.setStartAfter(start);
      range.setEndBefore(end);
      if (!range.collapsed) ranges.set(thread.id, range);
    }

    const at = end ?? start;
    if (!at?.parentNode) continue;
    const marker = root.ownerDocument.createElement('button');
    marker.type = 'button';
    marker.className = 'docx-review-marker';
    marker.setAttribute('data-comment-thread', thread.id);
    marker.setAttribute('data-author', String(authors.get(thread.comment.author) ?? 0));
    if (thread.resolved) marker.setAttribute('data-resolved', '');
    at.after(marker);
    anchors.set(`c:${thread.id}`, marker);
  }
}

const inPageOrder =
  (anchorOf: (key: string) => Node | undefined) =>
  (a: string, b: string): number => {
    const x = anchorOf(a);
    const y = anchorOf(b);
    if (!x || !y) return x ? -1 : y ? 1 : 0;
    if (x === y) return 0;
    return x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  };

/**
 * Decorate a rendered document in place: author colour, date and tooltip
 * on every tracked change; a numbered marker per comment thread; and the
 * range each thread covers, for the page to highlight.
 */
export function decorateReview(
  root: HTMLElement,
  revisions: readonly DocxRevision[],
  threads: readonly DocxCommentThread[],
  authors: ReadonlyMap<string, number>,
): DecoratedReview {
  const anchors = new Map<string, Element>();
  const ranges = new Map<string, Range>();
  decorateRevisions(root, revisions, authors, anchors);
  decorateComments(root, threads, authors, anchors, ranges);

  const order = inPageOrder((key) => anchors.get(key));
  const numbered = [...threads].sort((a, b) => order(`c:${a.id}`, `c:${b.id}`));
  const numbers = new Map(numbered.map((t, i) => [t.id, i + 1]));

  for (const thread of threads) {
    const marker = anchors.get(`c:${thread.id}`);
    if (!marker?.classList.contains('docx-review-marker')) continue;
    const number = numbers.get(thread.id) ?? 0;
    const replies = thread.replies.length;
    marker.textContent = String(number);
    marker.setAttribute(
      'aria-label',
      `Comment ${number} by ${thread.comment.author}` +
        (replies ? `, ${replies} ${replies === 1 ? 'reply' : 'replies'}` : '') +
        (thread.resolved ? ', resolved' : ''),
    );
    marker.setAttribute('title', `${thread.comment.author}: ${thread.comment.text}`);
  }

  return { anchors, ranges, numbers };
}

/**
 * Interleave threads and revisions in the order they appear in the page.
 * Anything with no place in the page goes last; ties keep
 * comments-before-changes (the sort is stable).
 */
export function orderReviewItems(
  threads: readonly DocxCommentThread[],
  revisions: readonly DocxRevision[],
  review: Pick<DecoratedReview, 'anchors' | 'numbers'>,
): DocxReviewItem[] {
  const order = inPageOrder((key) => review.anchors.get(key));
  const items: DocxReviewItem[] = [
    ...threads.map(
      (thread): DocxReviewItem => ({
        kind: 'comment',
        key: `c:${thread.id}`,
        number: review.numbers.get(thread.id) ?? 0,
        thread,
      }),
    ),
    ...revisions.map(
      (revision): DocxReviewItem => ({ kind: 'revision', key: `r:${revision.id}`, revision }),
    ),
  ];
  return items.sort((a, b) => order(a.key, b.key));
}
