/**
 * Content-types repair applied to a deck's bytes before `pptx-preview`
 * reads them. The stored file is never changed: only the copy the preview
 * parses.
 *
 * Why it exists: pptxgenjs, which Anthropic's pptx skill builds every new
 * deck with, writes one `/ppt/slideMasters/slideMaster{n}.xml` override
 * per *slide* into `[Content_Types].xml` but only ever writes
 * `slideMaster1.xml`. PowerPoint and LibreOffice skip an override whose
 * part is absent. `pptx-preview` 1.0.7 does not: it reads every listed
 * slide master inside one try block, the missing part throws, and it
 * resolves with zero slides instead of an error, so every pptxgenjs deck
 * of two or more slides "couldn't be read". Dropping overrides for parts
 * that are not in the archive fixes that and any other deck with the same
 * flaw, and leaves a well-formed deck untouched.
 */

/** The content-types part every OOXML package carries at its root. */
const CONTENT_TYPES = '[Content_Types].xml';

/** One `<Override .../>` element. The schema gives it no children. */
const OVERRIDE = /<Override\b[^>]*?\/>/g;
const PART_NAME = /\bPartName\s*=\s*(["'])(.*?)\1/;

/** Cached module, so a second preview doesn't re-request the chunk. */
let jszipPromise: Promise<import('jszip')> | null = null;

/**
 * `data` with every `[Content_Types].xml` override whose part is missing
 * from the archive removed.
 *
 * Returns `data` itself whenever there is nothing to do, including bytes
 * that are not a readable ZIP or carry no content-types part: those go on
 * to `pptx-preview` unchanged, so its own failure handling still applies.
 */
export async function dropOverridesForMissingParts(
  data: ArrayBuffer,
): Promise<ArrayBuffer> {
  try {
    jszipPromise ??= import('jszip').then((module) => module.default);
    const JSZip = await jszipPromise;
    const zip = await JSZip.loadAsync(data);

    const entry = zip.file(CONTENT_TYPES);
    if (!entry) return data;
    const xml = await entry.async('string');

    // Part names are case-insensitive in OPC, and a ZIP entry name has no
    // leading slash.
    const parts = new Set(
      Object.keys(zip.files).map((name) => name.toLowerCase()),
    );
    let dropped = 0;
    const repaired = xml.replace(OVERRIDE, (element: string) => {
      const name = PART_NAME.exec(element)?.[2];
      if (!name) return element;
      const path = safeDecode(name).replace(/^\//, '').toLowerCase();
      if (parts.has(path)) return element;
      dropped++;
      return '';
    });
    if (dropped === 0) return data;

    zip.file(CONTENT_TYPES, repaired);
    // STORE: the bytes only cross into the parser in this tab, so
    // compressing them again would cost time and buy nothing.
    return await zip.generateAsync({ type: 'arraybuffer', compression: 'STORE' });
  } catch {
    return data;
  }
}

function safeDecode(name: string): string {
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}
