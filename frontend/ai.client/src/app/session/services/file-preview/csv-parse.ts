/**
 * A delimited-text parser, sized for previewing a file rather than
 * ingesting one.
 *
 * Hand-rolled on purpose. The grid needs exactly one thing a library
 * would give us — RFC 4180 field splitting — and the rest of what a
 * parser package ships (streaming, workers, type coercion, header
 * mapping, a plugin surface) is weight we would carry in the bundle and
 * in `npm audit` forever. The whole of the specification that matters
 * here is: fields separated by a delimiter, rows by a newline, a field
 * may be quoted, a quote inside a quoted field is doubled, and a quoted
 * field may contain the delimiter or a newline.
 *
 * It is deliberately NOT a spreadsheet: no formulas, no types, no dates.
 * Every cell stays the string the file contained, because a preview that
 * silently reinterprets `007` as `7` or `3-4` as a date is lying about
 * the bytes the user is about to hand to something else.
 */

/** Delimiters sniffed for, in preference order on a tie. */
const CANDIDATE_DELIMITERS = [',', '\t', ';', '|'] as const;

export type CsvDelimiter = (typeof CANDIDATE_DELIMITERS)[number];

/**
 * Most bytes decoded and parsed.
 *
 * The file is already fully downloaded by the time we get here, so this
 * does not bound the transfer — it bounds the main-thread parse and the
 * DOM behind it. 10 MB of CSV is on the order of 100k rows, far past
 * what anyone reads in a preview pane, and parses in well under a
 * second. Past it we take the leading slice and say so.
 */
export const CSV_MAX_BYTES = 10 * 1024 * 1024;

/**
 * Most rows retained.
 *
 * A second, independent stop from `CSV_MAX_BYTES`: a file of very short
 * rows can stay under the byte cap and still produce enough rows to make
 * even a virtualised grid's backing array unpleasant.
 */
export const CSV_MAX_ROWS = 50_000;

/**
 * Most columns retained.
 *
 * Guards against a file whose quoting is broken badly enough that a
 * whole document collapses into one row of many thousands of fields —
 * which would otherwise become a `grid-template-columns` with that many
 * tracks.
 */
export const CSV_MAX_COLUMNS = 256;

/** What the grid renders. */
export interface CsvTable {
  /** First row of the file, used as column labels. */
  headers: string[];
  /** Every row after the header, padded to `headers.length`. */
  rows: string[][];
  /** The delimiter that was sniffed. Shown in the pane's subtitle. */
  delimiter: CsvDelimiter;
  /** True when a cap stopped us short of the end of the file. */
  truncated: boolean;
  /** Which cap fired, for the notice the pane shows. */
  truncatedBy: 'bytes' | 'rows' | 'columns' | null;
}

/** The file could not be read as delimited text at all. */
export class CsvParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CsvParseError';
  }
}

/**
 * Decode bytes to text, honouring a UTF-8 BOM and surviving a file that
 * is not valid UTF-8.
 *
 * `fatal: false` is the point: CSV exported from Excel on Windows is
 * routinely Windows-1252, and a preview that refuses the file outright
 * is worse than one that shows `�` where a smart quote was. The bytes on
 * S3 are untouched either way — this only affects what we draw.
 */
function decode(bytes: ArrayBuffer): string {
  const text = new TextDecoder('utf-8', { fatal: false }).decode(
    bytes.byteLength > CSV_MAX_BYTES ? bytes.slice(0, CSV_MAX_BYTES) : bytes,
  );
  // TextDecoder strips a leading BOM only for the exact `utf-8` label in
  // some engines and not others; strip it ourselves so a header never
  // renders with an invisible first character.
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

/**
 * Pick the delimiter by counting candidates in the first few lines,
 * ignoring anything inside quotes.
 *
 * Counting *consistency* rather than raw frequency is what makes this
 * reliable: a prose column full of commas can out-count the real `;`
 * delimiter on total occurrences, but only the real delimiter appears
 * the same number of times in every row.
 */
function sniffDelimiter(text: string): CsvDelimiter {
  const sample = sampleLines(text, 5);
  if (sample.length === 0) return ',';

  let best: CsvDelimiter = ',';
  let bestScore = -1;

  for (const candidate of CANDIDATE_DELIMITERS) {
    const counts = sample.map((line) => countOutsideQuotes(line, candidate));
    // A delimiter that never appears is not a delimiter.
    if (counts[0] === 0) continue;
    const consistent = counts.every((c) => c === counts[0]);
    // Consistency dominates; frequency breaks ties among consistent ones.
    const score = (consistent ? 1000 : 0) + counts[0];
    if (score > bestScore) {
      bestScore = score;
      best = candidate;
    }
  }

  return best;
}

/** First `max` physical lines, quote-awareness deliberately skipped —
 *  this is a sample for counting, not a parse. */
function sampleLines(text: string, max: number): string[] {
  const lines: string[] = [];
  let start = 0;
  while (lines.length < max && start < text.length) {
    let end = text.indexOf('\n', start);
    if (end === -1) end = text.length;
    const line = text.slice(start, end).replace(/\r$/, '');
    if (line.length > 0) lines.push(line);
    start = end + 1;
  }
  return lines;
}

function countOutsideQuotes(line: string, delimiter: string): number {
  let count = 0;
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      inQuotes = !inQuotes;
    } else if (!inQuotes && ch === delimiter) {
      count++;
    }
  }
  return count;
}

/**
 * Split delimited text into rows of fields.
 *
 * One pass, character at a time, tracking whether we are inside a quoted
 * field. `\r\n`, `\n` and a bare `\r` all end a row, because files
 * produced on all three platforms land here.
 */
function splitRows(
  text: string,
  delimiter: string,
): { rows: string[][]; hitRowCap: boolean } {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQuotes = false;
  let hitRowCap = false;

  const endField = (): void => {
    row.push(field);
    field = '';
  };
  const endRow = (): boolean => {
    row.push(field);
    field = '';
    // A trailing newline at end of file would otherwise add a row of one
    // empty field. Only drop it when the row is exactly that.
    if (!(row.length === 1 && row[0] === '')) rows.push(row);
    row = [];
    return rows.length >= CSV_MAX_ROWS;
  };

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];

    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
      continue;
    }

    if (ch === '"' && field === '') {
      // A quote only opens a quoted field at the start of one. Mid-field
      // quotes (`12" pipe`) are literal, which is what every spreadsheet
      // does with them.
      inQuotes = true;
    } else if (ch === delimiter) {
      endField();
    } else if (ch === '\n') {
      if (endRow()) {
        hitRowCap = true;
        break;
      }
    } else if (ch === '\r') {
      if (text[i + 1] === '\n') i++;
      if (endRow()) {
        hitRowCap = true;
        break;
      }
    } else {
      field += ch;
    }
  }

  // Whatever is left when the text runs out is a final row, unless the
  // file ended on a newline and left nothing behind.
  if (!hitRowCap && (field !== '' || row.length > 0)) endRow();

  return { rows, hitRowCap };
}

/**
 * Parse a delimited file into a rectangular table for the preview grid.
 *
 * Throws `CsvParseError` only when there is nothing to show at all. A
 * file that is merely *odd* — ragged rows, duplicate headers, a stray
 * quote — is rendered as-is, because the point of a preview is to show
 * the user what the file actually contains.
 */
export function parseCsv(bytes: ArrayBuffer): CsvTable {
  const truncatedByBytes = bytes.byteLength > CSV_MAX_BYTES;
  const text = decode(bytes);

  if (text.trim() === '') {
    throw new CsvParseError('This file is empty.');
  }

  const delimiter = sniffDelimiter(text);
  const { rows: rawRows, hitRowCap } = splitRows(text, delimiter);

  if (rawRows.length === 0) {
    throw new CsvParseError('This file has no rows to show.');
  }

  // The widest row decides the grid, so a row with extra fields is shown
  // in full rather than silently clipped to the header's width.
  const widest = rawRows.reduce((max, r) => Math.max(max, r.length), 0);
  const columnCount = Math.min(widest, CSV_MAX_COLUMNS);
  const hitColumnCap = widest > CSV_MAX_COLUMNS;

  const [headerRow, ...bodyRows] = rawRows;
  const headers = normalizeHeaders(headerRow, columnCount);
  const rows = bodyRows.map((r) => pad(r, columnCount));

  return {
    headers,
    rows,
    delimiter,
    truncated: truncatedByBytes || hitRowCap || hitColumnCap,
    truncatedBy: truncatedByBytes
      ? 'bytes'
      : hitRowCap
        ? 'rows'
        : hitColumnCap
          ? 'columns'
          : null,
  };
}

/** Pad or clip a row to the grid's width. */
function pad(row: string[], width: number): string[] {
  if (row.length === width) return row;
  if (row.length > width) return row.slice(0, width);
  return [...row, ...Array<string>(width - row.length).fill('')];
}

/**
 * Column labels, with a placeholder for any the file left blank.
 *
 * A blank header is common — an index column exported from pandas has
 * one — and an empty `<th>` gives a screen reader nothing to announce
 * when it reads a cell's column. The spreadsheet-style letter is the
 * familiar stand-in.
 */
function normalizeHeaders(row: string[], width: number): string[] {
  return pad(row, width).map(
    (h, i) => h.trim() || columnLetter(i),
  );
}

/** 0 -> A, 25 -> Z, 26 -> AA — the spreadsheet column naming. */
export function columnLetter(index: number): string {
  let n = index;
  let out = '';
  do {
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return out;
}
