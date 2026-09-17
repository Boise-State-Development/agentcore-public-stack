import { describe, expect, it } from 'vitest';
import {
  CSV_MAX_COLUMNS,
  CSV_MAX_ROWS,
  CsvParseError,
  columnLetter,
  parseCsv,
} from './csv-parse';

/** UTF-8 encode, since the parser takes the bytes the fetch returned. */
function bytes(text: string): ArrayBuffer {
  const encoded = new TextEncoder().encode(text);
  return encoded.buffer.slice(
    encoded.byteOffset,
    encoded.byteOffset + encoded.byteLength,
  ) as ArrayBuffer;
}

describe('parseCsv', () => {
  it('reads a plain comma-delimited file', () => {
    const table = parseCsv(bytes('name,qty\nwidget,3\ngadget,7\n'));

    expect(table.headers).toEqual(['name', 'qty']);
    expect(table.rows).toEqual([
      ['widget', '3'],
      ['gadget', '7'],
    ]);
    expect(table.delimiter).toBe(',');
    expect(table.truncated).toBe(false);
  });

  it('keeps cells as strings rather than coercing them', () => {
    // The whole reason the grid is not a spreadsheet: a preview that
    // renders 007 as 7 misrepresents what the file will hand downstream.
    const table = parseCsv(bytes('zip,ratio\n007,3-4\n'));

    expect(table.rows[0]).toEqual(['007', '3-4']);
  });

  describe('RFC 4180 quoting', () => {
    it('keeps a delimiter inside a quoted field', () => {
      const table = parseCsv(bytes('a,b\n"Boise, ID",2\n'));

      expect(table.rows[0]).toEqual(['Boise, ID', '2']);
    });

    it('keeps a newline inside a quoted field', () => {
      const table = parseCsv(bytes('a,b\n"line one\nline two",2\n'));

      expect(table.rows).toHaveLength(1);
      expect(table.rows[0][0]).toBe('line one\nline two');
    });

    it('unescapes a doubled quote', () => {
      const table = parseCsv(bytes('a\n"she said ""hi"""\n'));

      expect(table.rows[0][0]).toBe('she said "hi"');
    });

    it('treats a mid-field quote as literal', () => {
      // `12" pipe` is not an opening quote — spreadsheets read it as text.
      const table = parseCsv(bytes('part\n12" pipe\n'));

      expect(table.rows[0][0]).toBe('12" pipe');
    });
  });

  describe('delimiter sniffing', () => {
    it('picks the tab in a TSV', () => {
      const table = parseCsv(bytes('name\tqty\nwidget\t3\n'));

      expect(table.delimiter).toBe('\t');
      expect(table.headers).toEqual(['name', 'qty']);
    });

    it('picks the semicolon over commas inside prose', () => {
      // The comma occurs more often overall, but only the semicolon
      // occurs the same number of times in every row.
      const table = parseCsv(
        bytes(
          'title;note\n' +
            'First;"a, b, c, d"\n' +
            'Second;"e, f, g, h"\n' +
            'Third;"i, j, k, l"\n',
        ),
      );

      expect(table.delimiter).toBe(';');
      expect(table.headers).toEqual(['title', 'note']);
      expect(table.rows[0]).toEqual(['First', 'a, b, c, d']);
    });

    it('falls back to a comma for a single-column file', () => {
      const table = parseCsv(bytes('name\nwidget\ngadget\n'));

      expect(table.delimiter).toBe(',');
      expect(table.headers).toEqual(['name']);
      expect(table.rows).toEqual([['widget'], ['gadget']]);
    });
  });

  describe('line endings', () => {
    it.each([
      ['CRLF', 'a,b\r\n1,2\r\n'],
      ['LF', 'a,b\n1,2\n'],
      ['CR', 'a,b\r1,2\r'],
    ])('handles %s', (_label, text) => {
      const table = parseCsv(bytes(text));

      expect(table.headers).toEqual(['a', 'b']);
      expect(table.rows).toEqual([['1', '2']]);
    });

    it('does not add an empty row for a trailing newline', () => {
      expect(parseCsv(bytes('a\n1\n')).rows).toEqual([['1']]);
    });

    it('reads a final row with no trailing newline', () => {
      expect(parseCsv(bytes('a\n1')).rows).toEqual([['1']]);
    });
  });

  describe('ragged rows', () => {
    it('pads a short row to the grid width', () => {
      const table = parseCsv(bytes('a,b,c\n1,2\n'));

      expect(table.rows[0]).toEqual(['1', '2', '']);
    });

    it('widens the grid for a row with extra fields', () => {
      // Showing the stray field is the point — clipping it to the
      // header's width would hide the malformed row we are previewing.
      const table = parseCsv(bytes('a,b\n1,2,3\n'));

      expect(table.headers).toEqual(['a', 'b', 'C']);
      expect(table.rows[0]).toEqual(['1', '2', '3']);
    });
  });

  describe('headers', () => {
    it('substitutes a spreadsheet letter for a blank header', () => {
      const table = parseCsv(bytes(',name\n0,widget\n'));

      expect(table.headers).toEqual(['A', 'name']);
    });

    it('trims surrounding whitespace', () => {
      expect(parseCsv(bytes(' name , qty \n1,2\n')).headers).toEqual([
        'name',
        'qty',
      ]);
    });
  });

  it('strips a UTF-8 BOM from the first header', () => {
    const table = parseCsv(bytes('﻿name,qty\nwidget,3\n'));

    expect(table.headers).toEqual(['name', 'qty']);
  });

  it('renders a header-only file as an empty grid', () => {
    const table = parseCsv(bytes('name,qty\n'));

    expect(table.headers).toEqual(['name', 'qty']);
    expect(table.rows).toEqual([]);
  });

  describe('caps', () => {
    it('stops at the row cap and reports it', () => {
      const rows = Array.from(
        { length: CSV_MAX_ROWS + 50 },
        (_, i) => `${i},x`,
      ).join('\n');
      const table = parseCsv(bytes(`a,b\n${rows}\n`));

      expect(table.rows).toHaveLength(CSV_MAX_ROWS - 1); // header took one
      expect(table.truncated).toBe(true);
      expect(table.truncatedBy).toBe('rows');
    });

    it('stops at the column cap and reports it', () => {
      const wide = Array.from({ length: CSV_MAX_COLUMNS + 10 }, (_, i) =>
        String(i),
      ).join(',');
      const table = parseCsv(bytes(`${wide}\n${wide}\n`));

      expect(table.headers).toHaveLength(CSV_MAX_COLUMNS);
      expect(table.rows[0]).toHaveLength(CSV_MAX_COLUMNS);
      expect(table.truncated).toBe(true);
      expect(table.truncatedBy).toBe('columns');
    });
  });

  describe('failures', () => {
    it('rejects an empty file', () => {
      expect(() => parseCsv(bytes(''))).toThrow(CsvParseError);
    });

    it('rejects a whitespace-only file', () => {
      expect(() => parseCsv(bytes('\n\n  \n'))).toThrow(CsvParseError);
    });
  });

  it('survives bytes that are not valid UTF-8', () => {
    // A Windows-1252 smart quote (0x92) is invalid UTF-8. Showing a
    // replacement character beats refusing the file.
    const raw = new Uint8Array([
      0x61, 0x0a, 0x92, 0x78, // "a\n<0x92>x"
    ]);
    const table = parseCsv(raw.buffer as ArrayBuffer);

    expect(table.headers).toEqual(['a']);
    expect(table.rows[0][0]).toContain('x');
  });
});

describe('columnLetter', () => {
  it.each([
    [0, 'A'],
    [25, 'Z'],
    [26, 'AA'],
    [51, 'AZ'],
    [52, 'BA'],
  ])('maps %i to %s', (index, expected) => {
    expect(columnLetter(index)).toBe(expected);
  });
});
