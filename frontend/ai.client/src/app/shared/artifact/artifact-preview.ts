/**
 * Reading an artifact's stored excerpt (`apis/app_api/artifacts/preview.py`)
 * into something a card can draw. Everything here produces plain text for
 * interpolation; nothing is ever bound as HTML.
 */

export type PreviewKind = 'markdown' | 'csv' | 'page' | 'svg' | 'text';

export type MarkdownBlock =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'item'; text: string }
  | { kind: 'para'; text: string };

/** Which drawing a content type gets. */
export function previewKind(contentType: string): PreviewKind {
  const bare = contentType.split(';')[0].trim().toLowerCase();
  if (bare === 'text/markdown' || bare === 'text/x-markdown') return 'markdown';
  if (bare === 'text/csv') return 'csv';
  if (bare === 'image/svg+xml') return 'svg';
  if (bare === 'text/html' || bare === 'application/xhtml+xml') return 'page';
  return 'text';
}

/** Markdown inline syntax read as the words it marks. */
export function plainInline(text: string): string {
  const inline = text
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/(^|[^\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])/g, '$1$2')
    .replace(/`([^`]*)`/g, '$1');
  return stripTags(inline).trim();
}

/**
 * Drops every `<…>` span and every `<` that never closes, keeping the words
 * around them. A scan rather than a regex replace, so no tag start can be
 * rebuilt from the pieces a single pass leaves behind.
 */
function stripTags(text: string): string {
  let out = '';
  let pending: string | null = null;
  for (const ch of text) {
    if (ch === '<') {
      if (pending !== null) out += pending;
      pending = '';
    } else if (pending === null) {
      out += ch;
    } else if (ch === '>') {
      pending = null;
    } else {
      pending += ch;
    }
  }
  return pending === null ? out : out + pending;
}

/** Headings, list items and paragraphs: the shapes a few hundred characters of Markdown make. */
export function markdownBlocks(source: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let para: string[] = [];
  const flush = () => {
    const text = plainInline(para.join(' '));
    if (text) blocks.push({ kind: 'para', text });
    para = [];
  };
  for (const raw of source.split('\n')) {
    const line = raw.trim();
    if (!line || /^(`{3,}|~{3,})/.test(line) || /^([-*_]\s*){3,}$/.test(line) || /^\|?[\s:|-]+\|[\s:|-]*$/.test(line)) {
      flush();
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const text = plainInline(heading[2]);
      if (text) blocks.push({ kind: 'heading', level: heading[1].length, text });
      continue;
    }
    const item = /^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)$/.exec(line);
    if (item) {
      flush();
      const text = plainInline(item[1]);
      if (text) blocks.push({ kind: 'item', text });
      continue;
    }
    const cells = line.startsWith('|') ? line.replace(/^\||\|$/g, '').split('|').map(c => c.trim()).join(' · ') : line;
    para.push(cells.replace(/^>\s?/, ''));
  }
  flush();
  return blocks;
}

/** One CSV line's cells, honouring double-quoted fields. */
export function csvCells(line: string): string[] {
  const cells: string[] = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (ch === '"') {
        quoted = false;
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ',') {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += ch;
    }
  }
  cells.push(cell.trim());
  return cells;
}

/** The header and first rows, cut to what fits on a card. */
export function csvTable(source: string, maxRows = 6, maxCols = 4): { header: string[]; rows: string[][] } {
  const lines = source.split(/\r?\n/).filter(l => l.trim());
  const [header = [], ...rows] = lines.slice(0, maxRows + 1).map(l => csvCells(l).slice(0, maxCols));
  return { header, rows };
}

/** An SVG as an `<img>` source, where its scripts never run and it loads nothing. */
export function svgDataUri(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** Whether a figure reads as a number, so its column aligns right. */
export function isNumeric(cell: string): boolean {
  return /^[-+]?[$€£]?\d[\d,]*(\.\d+)?%?$/.test(cell);
}
