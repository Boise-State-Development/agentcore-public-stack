import { describe, expect, it } from 'vitest';
import {
  DocxModelNode,
  authorIndex,
  commentParaIds,
  decorateReview,
  orderReviewItems,
  tagRevisions,
  threadComments,
} from './docx-review';

const text = (t: string): DocxModelNode => ({ type: 'text', text: t });
const run = (t: string): DocxModelNode => ({ type: 'run', children: [text(t)] });
const deletedRun = (t: string): DocxModelNode => ({
  type: 'run',
  children: [{ type: 'deletedText', text: t }],
});
const ins = (t: string, author: string, date = '2026-11-12T17:20:00Z'): DocxModelNode => ({
  type: 'inserted',
  author,
  date,
  children: [run(t)],
});
const del = (t: string, author: string, date = '2026-11-12T17:20:00Z'): DocxModelNode => ({
  type: 'deleted',
  author,
  date,
  children: [deletedRun(t)],
});
const para = (...children: DocxModelNode[]): DocxModelNode => ({ type: 'paragraph', children });
const rangeStart = (id: string): DocxModelNode => ({ type: 'commentRangeStart', id });
const rangeEnd = (id: string): DocxModelNode => ({ type: 'commentRangeEnd', id });

/**
 * Paint a model the way docx-preview 0.4.0 does, for the parts the review
 * reads: a change as `<ins>`/`<del>` carrying only `dateTime: node.date`,
 * a comment range as two HTML comment nodes, and a comment reference as
 * nothing at all (the library's `h('#fragment')` drops its children).
 */
function paint(body: DocxModelNode): HTMLElement {
  const root = document.createElement('div');
  const page = document.createElement('section');
  page.className = 'docx';
  root.appendChild(page);
  const walk = (node: DocxModelNode, into: Node): void => {
    switch (node.type) {
      case 'paragraph': {
        const p = document.createElement('p');
        into.appendChild(p);
        node.children?.forEach((c) => walk(c, p));
        return;
      }
      case 'inserted':
      case 'deleted': {
        const el = document.createElement(node.type === 'inserted' ? 'ins' : 'del');
        (el as HTMLModElement).dateTime = node.date ?? '';
        into.appendChild(el);
        node.children?.forEach((c) => walk(c, el));
        return;
      }
      case 'run': {
        const span = document.createElement('span');
        into.appendChild(span);
        node.children?.forEach((c) => walk(c, span));
        return;
      }
      case 'text':
      case 'deletedText':
        into.appendChild(document.createTextNode(node.text ?? ''));
        return;
      case 'commentRangeStart':
        into.appendChild(document.createComment(`start of comment #${node.id}`));
        return;
      case 'commentRangeEnd':
        into.appendChild(document.createComment(`end of comment #${node.id}`));
        return;
      default:
        node.children?.forEach((c) => walk(c, into));
    }
  };
  body.children?.forEach((c) => walk(c, page));
  return root;
}

const W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
const W14 = 'http://schemas.microsoft.com/office/word/2010/wordml';
const commentsXml = (entries: [id: string, paraId: string | null][]): string =>
  `<w:comments xmlns:w="${W}" xmlns:w14="${W14}">` +
  entries
    .map(
      ([id, paraId]) =>
        `<w:comment w:id="${id}"><w:p${paraId ? ` w14:paraId="${paraId}"` : ''}><w:r><w:t>x</w:t></w:r></w:p></w:comment>`,
    )
    .join('') +
  '</w:comments>';

const comment = (id: string, author: string, words: string): DocxModelNode => ({
  id,
  author,
  date: '2026-11-01T09:30:00Z',
  children: [para(run(words))],
});

describe('tagRevisions', () => {
  it('lists insertions and deletions in document order with their author and date', () => {
    const body: DocxModelNode = {
      children: [para(run('Pay in '), ins('thirty', 'Dana')), para(del('Late fees apply.', 'Assistant'))],
    };

    const revisions = tagRevisions(body);

    expect(revisions).toEqual([
      expect.objectContaining({ kind: 'insert', author: 'Dana', inserted: 'thirty', deleted: '' }),
      expect.objectContaining({ kind: 'delete', author: 'Assistant', deleted: 'Late fees apply.' }),
    ]);
    expect(revisions[0].date).toBe('2026-11-12T17:20:00Z');
  });

  it('reads a deletion followed directly by an insertion from the same author as one replacement', () => {
    // The shape redline_word_document writes for a `replace`.
    const body: DocxModelNode = {
      children: [para(run('Governed by '), del('Idaho', 'Assistant'), ins('Delaware', 'Assistant'))],
    };

    const revisions = tagRevisions(body);

    expect(revisions).toHaveLength(1);
    expect(revisions[0]).toEqual(
      expect.objectContaining({ kind: 'replace', deleted: 'Idaho', inserted: 'Delaware' }),
    );
  });

  it('keeps a deletion and insertion apart when different people made them, or text sits between', () => {
    const body: DocxModelNode = {
      children: [
        para(del('forty-five', 'Dana'), ins('thirty', 'Sam')),
        para(del('old', 'Dana'), run(' kept '), ins('new', 'Dana')),
      ],
    };

    expect(tagRevisions(body).map((r) => r.kind)).toEqual(['delete', 'insert', 'delete', 'insert']);
  });

  it('names a change with no author rather than leaving it blank', () => {
    const body: DocxModelNode = { children: [para({ type: 'inserted', children: [run('x')] })] };

    expect(tagRevisions(body)[0].author).toBe('Unknown author');
  });

  it('returns nothing for a document without tracked changes', () => {
    expect(tagRevisions({ children: [para(run('plain'))] })).toEqual([]);
    expect(tagRevisions(undefined)).toEqual([]);
  });
});

describe('threadComments', () => {
  it('threads replies under their parent and carries resolved state from commentsExtended', () => {
    const threads = threadComments(
      [
        comment('1', 'Dana', 'Twelve months is short.'),
        comment('2', 'Sam', 'Agreed.'),
        comment('3', 'Sam', 'Standard clause.'),
      ],
      commentParaIds(commentsXml([['1', 'AA'], ['2', 'BB'], ['3', 'CC']])),
      { AA: { done: false }, BB: { paraIdParent: 'AA' }, CC: { done: true } },
    );

    expect(threads).toHaveLength(2);
    expect(threads[0].comment.text).toBe('Twelve months is short.');
    expect(threads[0].replies.map((r) => r.text)).toEqual(['Agreed.']);
    expect(threads[0].resolved).toBe(false);
    expect(threads[1].resolved).toBe(true);
  });

  it('follows a reply to a reply back to the thread it belongs to', () => {
    const threads = threadComments(
      [comment('1', 'A', 'root'), comment('2', 'B', 'reply'), comment('3', 'A', 'reply to reply')],
      commentParaIds(commentsXml([['1', 'AA'], ['2', 'BB'], ['3', 'CC']])),
      { BB: { paraIdParent: 'AA' }, CC: { paraIdParent: 'BB' } },
    );

    expect(threads).toHaveLength(1);
    expect(threads[0].replies.map((r) => r.id)).toEqual(['2', '3']);
  });

  it('makes every comment its own thread when the file has no commentsExtended part', () => {
    // Older Word, LibreOffice and our own redline tool write no paraIds.
    const threads = threadComments(
      [comment('7', 'Assistant', 'one'), comment('8', 'Assistant', 'two')],
      commentParaIds(commentsXml([['7', null], ['8', null]])),
      undefined,
    );

    expect(threads.map((t) => [t.id, t.replies.length, t.resolved])).toEqual([
      ['7', 0, false],
      ['8', 0, false],
    ]);
  });

  it('survives a parent link that loops back on itself', () => {
    const threads = threadComments(
      [comment('1', 'A', 'a'), comment('2', 'B', 'b')],
      commentParaIds(commentsXml([['1', 'AA'], ['2', 'BB']])),
      { AA: { paraIdParent: 'BB' }, BB: { paraIdParent: 'AA' } },
    );

    expect(threads.map((t) => [t.id, t.replies.length])).toEqual([
      ['1', 0],
      ['2', 0],
    ]);
  });
});

describe('decorateReview', () => {
  function render(body: DocxModelNode, comments: DocxModelNode[] = []) {
    const revisions = tagRevisions(body);
    const threads = threadComments(comments, new Map(), undefined);
    const root = paint(body);
    const authors = authorIndex(revisions, threads);
    const decorated = decorateReview(root, revisions, threads, authors);
    return { root, revisions, threads, decorated };
  }

  it("puts each change's real date back and names who made it", () => {
    const { root, revisions } = render({
      children: [para(del('Idaho', 'Assistant', '2026-11-12T17:20:00Z'), ins('Delaware', 'Assistant'))],
    });

    const [d, i] = [root.querySelector('del')!, root.querySelector('ins')!];
    expect(d.getAttribute('datetime')).toBe('2026-11-12T17:20:00Z');
    expect(d.getAttribute('data-revision')).toBe(revisions[0].id);
    expect(i.getAttribute('data-revision')).toBe(revisions[0].id);
    expect(d.getAttribute('title')).toMatch(/^Deleted by Assistant · /);
    expect(i.getAttribute('title')).toMatch(/^Inserted by Assistant · /);
  });

  it('gives each author a colour slot of their own', () => {
    const { root } = render({ children: [para(ins('a', 'Dana'), run(' '), ins('b', 'Sam'))] });

    const slots = Array.from(root.querySelectorAll('ins')).map((e) => e.getAttribute('data-author'));
    expect(slots).toEqual(['0', '1']);
  });

  it('draws a numbered marker at the end of each commented range, in page order', () => {
    const { root, decorated } = render(
      {
        children: [
          para(rangeStart('9'), run('later in the file'), rangeEnd('9')),
          para(rangeStart('4'), run('earlier'), rangeEnd('4')),
        ],
      },
      // Listed out of page order on purpose: numbering follows the page.
      [comment('4', 'Sam', 'b'), comment('9', 'Dana', 'a')],
    );

    const markers = Array.from(root.querySelectorAll('.docx-review-marker'));
    expect(markers.map((m) => [m.getAttribute('data-comment-thread'), m.textContent])).toEqual([
      ['9', '1'],
      ['4', '2'],
    ]);
    // Right after the range's end, where Word puts the reference run.
    expect(markers[0].previousSibling?.textContent).toBe('end of comment #9');
    expect(markers[0].previousElementSibling?.textContent).toBe('later in the file');
    expect(markers[0].getAttribute('aria-label')).toBe('Comment 1 by Dana');
    expect(decorated.ranges.get('9')?.toString()).toBe('later in the file');
  });

  it('removes the library glyph and popover if a later version renders them', () => {
    const body: DocxModelNode = { children: [para(rangeStart('1'), run('x'), rangeEnd('1'))] };
    const revisions = tagRevisions(body);
    const threads = threadComments([comment('1', 'A', 'c')], new Map(), undefined);
    const root = paint(body);
    const glyph = document.createElement('span');
    glyph.className = 'docx-comment-ref';
    const popover = document.createElement('div');
    popover.className = 'docx-comment-popover';
    root.querySelector('p')!.append(glyph, popover);

    decorateReview(root, revisions, threads, authorIndex(revisions, threads));

    expect(root.querySelector('.docx-comment-ref, .docx-comment-popover')).toBeNull();
    expect(root.querySelectorAll('.docx-review-marker')).toHaveLength(1);
  });
});

describe('orderReviewItems', () => {
  it('interleaves comments and changes in the order they appear in the page', () => {
    const body: DocxModelNode = {
      children: [
        para(ins('first', 'Dana')),
        para(rangeStart('5'), run('second'), rangeEnd('5')),
        para(del('third', 'Sam')),
      ],
    };
    const revisions = tagRevisions(body);
    const threads = threadComments([comment('5', 'Sam', 'note')], new Map(), undefined);
    const root = paint(body);
    const decorated = decorateReview(root, revisions, threads, authorIndex(revisions, threads));

    const items = orderReviewItems(threads, revisions, decorated);

    expect(items.map((i) => i.key)).toEqual(['r:1', 'c:5', 'r:2']);
    expect(items[1]).toEqual(expect.objectContaining({ kind: 'comment', number: 1 }));
  });
});
