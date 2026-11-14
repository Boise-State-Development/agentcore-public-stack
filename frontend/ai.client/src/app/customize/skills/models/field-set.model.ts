/**
 * Field-set models — the TypeScript mirror of `apis/app_api/skills/field_sets.py`
 * (`FieldSetResponse`, `FieldSetWriteRequest`, `FieldSetListResponse`). Shapes
 * must stay in sync with that module (CLAUDE.md cross-package contract).
 *
 * The SPA never parses or writes the field-set file (field-set-authoring D2):
 * it trades this JSON, and the backend owns the markdown in both directions.
 * Nothing here knows a column alias or a cell rule. Even the 14-field ceiling
 * arrives as `maxFields` rather than being copied into a constant.
 */

/** Every type a field may declare, in the order the editor offers them. */
export const FIELD_TYPES = ['string', 'number', 'currency', 'date', 'boolean', 'enum', 'list'] as const;
export type FieldSetFieldType = (typeof FIELD_TYPES)[number];

export const FIELD_TYPE_LABELS: Record<FieldSetFieldType, string> = {
  string: 'Text',
  number: 'Number',
  currency: 'Currency',
  date: 'Date',
  boolean: 'Yes / no',
  enum: 'One of a list',
  list: 'List of text',
};

export interface FieldSetField {
  name: string;
  title: string;
  /** A string, not `FieldSetFieldType`: a hand-written file may carry a type
   *  this build does not know, and the form must show it rather than drop it. */
  type: string;
  find: string;
  enum: string[];
  optional: boolean;
  /** This field's cells for the set's `unmanagedColumns`, in that order. */
  unmanagedValues: string[];
}

/** One set as `GET`/`PUT /skills/mine/{id}/field-sets/{name}` returns it. */
export interface FieldSet {
  skillId: string;
  name: string;
  filename: string;
  title: string;
  version: string;
  fields: FieldSetField[];
  /** `## Rules`, verbatim. Shown read-only and sent back unchanged. */
  rules: string | null;
  /** Prose before the table. Carried, not edited, by the form. */
  preamble: string | null;
  /** Prose after the table. Carried, not edited, by the form. */
  postscript: string | null;
  /** Frontmatter lines other than `title` and `version`. Carried. */
  frontmatter: string[];
  /** Columns the parser does not read (a note column). Carried. */
  unmanagedColumns: string[];
  /** The stored file, or the one a validate-only `PUT` would store. */
  markdown: string;
  /** The parser's measured ceiling. */
  maxFields: number;
}

export interface FieldSetSummary {
  name: string;
  filename: string;
  size: number;
  contentHash: string;
}

export interface FieldSetListResponse {
  skillId: string;
  fieldSets: FieldSetSummary[];
  maxFields: number;
}

/** The structured `PUT` body. Everything the form does not edit rides along. */
export interface FieldSetStructuredWrite {
  title: string;
  version: string;
  fields: FieldSetField[];
  rules: string | null;
  preamble: string | null;
  postscript: string | null;
  frontmatter: string[];
  unmanagedColumns: string[];
}

export interface FieldSetMarkdownWrite {
  markdown: string;
}

export type FieldSetWrite = FieldSetStructuredWrite | FieldSetMarkdownWrite;

/**
 * Why a stored set could not be opened as a form: the parser's message and,
 * when the bytes were text, the markdown itself, so it can still be fixed.
 */
export interface FieldSetReadError {
  message: string;
  markdown: string | null;
}

/** The bundle filename a set named `name` is stored under. */
export function fieldSetFilename(name: string): string {
  return `field-set.${name}.md`;
}

/** The set name a bundle filename declares, or `null` if it declares none. */
export function fieldSetNameFor(filename: string): string | null {
  const match = /^field-set\.([a-z0-9]+(?:-[a-z0-9]+)*)\.md$/.exec(filename);
  return match ? match[1] : null;
}

/**
 * A set name from a title: lowercase words joined by hyphens. A suggestion for
 * a new set's name only. The backend refuses an invalid name with its own
 * message, so this never has to be the authority.
 */
export function suggestSetName(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

/** A field name from its title: `snake_case`, starting with a letter. */
export function suggestFieldName(title: string): string {
  const snake = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return /^[a-z]/.test(snake) ? snake : snake ? `field_${snake}` : '';
}

/** The structured body for a set as loaded, unchanged. */
export function toStructuredWrite(set: FieldSet): FieldSetStructuredWrite {
  return {
    title: set.title,
    version: set.version,
    fields: set.fields.map((f) => ({ ...f, enum: [...f.enum], unmanagedValues: [...f.unmanagedValues] })),
    rules: set.rules,
    preamble: set.preamble,
    postscript: set.postscript,
    frontmatter: [...set.frontmatter],
    unmanagedColumns: [...set.unmanagedColumns],
  };
}

/**
 * What decides the generated schema's shape: each field's name, type and
 * allowed values, in order. A changed `find` or title is not a new shape
 * (field-set-authoring §6), so neither suggests a version bump.
 */
export function fieldSetShape(fields: readonly Pick<FieldSetField, 'name' | 'type' | 'enum' | 'optional'>[]): string {
  return JSON.stringify(fields.map((f) => [f.name, f.type, f.enum, f.optional]));
}
