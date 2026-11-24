/**
 * Repairs applied to a deck's bytes before `pptx-preview` reads them, for
 * flaws PowerPoint and LibreOffice tolerate and `pptx-preview` 1.0.7 does
 * not. The stored file is never changed: only the copy the preview parses.
 *
 * Both flaws show up in every deck Anthropic's pptx skill builds, because
 * it builds with pptxgenjs:
 *
 * 1. **Content types naming parts that don't exist.** pptxgenjs writes one
 *    `/ppt/slideMasters/slideMaster{n}.xml` override per *slide* into
 *    `[Content_Types].xml` but only ever writes `slideMaster1.xml`.
 *    `pptx-preview` reads every listed slide master inside one try block,
 *    the missing part throws, and it resolves with zero slides, so the
 *    deck "couldn't be read". Overrides for absent parts are dropped.
 * 2. **Colour-map aliases in a layout's background.** `pptx-preview`
 *    resolves a slide's or master's background scheme colour through the
 *    colour map, but a *layout's* background without it (`g(fill, theme)`,
 *    missing the third argument its siblings pass). An alias such as
 *    `tx2` therefore resolves to nothing and the slide paints white, and
 *    pptxgenjs puts its dark backgrounds on a layout (`schemeClr tx2`).
 *    Inside each layout's `<p:bg>`, aliases are rewritten to the colour the
 *    layout's own override mapping names, or else its master's `clrMap`.
 *    That is exactly what PowerPoint resolves, so the colour is unchanged.
 */

/** The content-types part every OOXML package carries at its root. */
const CONTENT_TYPES = '[Content_Types].xml';

/** One `<Override .../>` element. The schema gives it no children. */
const OVERRIDE = /<Override\b[^>]*?\/>/g;
const PART_NAME = /\bPartName\s*=\s*(["'])(.*?)\1/;

const SLIDE_LAYOUT = /^ppt\/slideLayouts\/[^/]+\.xml$/i;
const LAYOUT_BACKGROUND = /<p:bg\b[^>]*>[\s\S]*?<\/p:bg>/;
const SCHEME_COLOR = /(<a:schemeClr\b[^>]*?\bval=")([^"]+)(")/g;
const MASTER_COLOR_MAP = /<p:clrMap\b([^>]*?)\/?>/;
const OVERRIDE_COLOR_MAP = /<a:overrideClrMapping\b([^>]*?)\/?>/;
const ATTRIBUTE = /([\w:]+)\s*=\s*"([^"]*)"/g;
const SLIDE_MASTER_RELATIONSHIP = /\/relationships\/slideMaster$/;

type Zip = Awaited<ReturnType<import('jszip')['loadAsync']>>;

/** Cached module, so a second preview doesn't re-request the chunk. */
let jszipPromise: Promise<import('jszip')> | null = null;

/**
 * `data` with both repairs applied.
 *
 * Returns `data` itself whenever there is nothing to repair, including
 * bytes that are not a readable ZIP: those go on to `pptx-preview`
 * unchanged, so its own failure handling still applies.
 */
export async function repairDeckForPreview(data: ArrayBuffer): Promise<ArrayBuffer> {
  try {
    jszipPromise ??= import('jszip').then((module) => module.default);
    const JSZip = await jszipPromise;
    const zip = await JSZip.loadAsync(data);

    const changedContentTypes = await dropOverridesForMissingParts(zip);
    const changedLayouts = await resolveLayoutBackgroundAliases(zip);
    if (!changedContentTypes && !changedLayouts) return data;

    // STORE: the bytes only cross into the parser in this tab, so
    // compressing them again would cost time and buy nothing.
    return await zip.generateAsync({ type: 'arraybuffer', compression: 'STORE' });
  } catch {
    return data;
  }
}

/** Repair 1. Whether `[Content_Types].xml` changed. */
async function dropOverridesForMissingParts(zip: Zip): Promise<boolean> {
  const entry = zip.file(CONTENT_TYPES);
  if (!entry) return false;
  const xml = await entry.async('string');

  // Part names are case-insensitive in OPC, and a ZIP entry name has no
  // leading slash.
  const parts = new Set(Object.keys(zip.files).map((name) => name.toLowerCase()));
  let dropped = 0;
  const repaired = xml.replace(OVERRIDE, (element: string) => {
    const name = PART_NAME.exec(element)?.[2];
    if (!name) return element;
    const path = safeDecode(name).replace(/^\//, '').toLowerCase();
    if (parts.has(path)) return element;
    dropped++;
    return '';
  });
  if (dropped === 0) return false;

  zip.file(CONTENT_TYPES, repaired);
  return true;
}

/** Repair 2. Whether any layout changed. */
async function resolveLayoutBackgroundAliases(zip: Zip): Promise<boolean> {
  const layouts = Object.keys(zip.files).filter((name) => SLIDE_LAYOUT.test(name));
  const masterMaps = new Map<string, Record<string, string> | null>();
  let changed = false;

  for (const layout of layouts) {
    const xml = await zip.file(layout)!.async('string');
    const background = LAYOUT_BACKGROUND.exec(xml)?.[0];
    if (!background || !background.includes('<a:schemeClr')) continue;

    const colorMap =
      layoutOverrideMap(xml) ?? (await masterColorMap(zip, layout, masterMaps));
    if (!colorMap) continue;

    const resolved = background.replace(
      SCHEME_COLOR,
      (match: string, head: string, value: string, tail: string) => {
        const mapped = colorMap[value];
        return mapped && mapped !== value ? head + mapped + tail : match;
      },
    );
    if (resolved === background) continue;

    zip.file(layout, xml.replace(background, () => resolved));
    changed = true;
  }
  return changed;
}

/** A layout's own `<a:overrideClrMapping>`, which wins over its master's. */
function layoutOverrideMap(layoutXml: string): Record<string, string> | null {
  const attributes = OVERRIDE_COLOR_MAP.exec(layoutXml)?.[1];
  return attributes ? parseAttributes(attributes) : null;
}

/** The `<p:clrMap>` of the master a layout's rels point at, memoised per master. */
async function masterColorMap(
  zip: Zip,
  layout: string,
  cache: Map<string, Record<string, string> | null>,
): Promise<Record<string, string> | null> {
  const slash = layout.lastIndexOf('/');
  const rels = zip.file(`${layout.slice(0, slash)}/_rels/${layout.slice(slash + 1)}.rels`);
  if (!rels) return null;

  let master: string | null = null;
  for (const element of (await rels.async('string')).match(/<Relationship\b[^>]*>/g) ?? []) {
    const attributes = parseAttributes(element);
    if (SLIDE_MASTER_RELATIONSHIP.test(attributes['Type'] ?? '') && attributes['Target']) {
      master = resolvePart(layout, attributes['Target']);
      break;
    }
  }
  if (!master) return null;

  if (!cache.has(master)) {
    const file = zip.file(master);
    const attributes = file
      ? MASTER_COLOR_MAP.exec(await file.async('string'))?.[1]
      : undefined;
    cache.set(master, attributes ? parseAttributes(attributes) : null);
  }
  return cache.get(master) ?? null;
}

function parseAttributes(source: string): Record<string, string> {
  const attributes: Record<string, string> = {};
  for (const [, name, value] of source.matchAll(ATTRIBUTE)) attributes[name] = value;
  return attributes;
}

/** A relationship target, relative to the part that owns the rels, as a ZIP entry name. */
function resolvePart(source: string, target: string): string {
  const segments = target.startsWith('/')
    ? []
    : source.split('/').slice(0, -1);
  for (const segment of safeDecode(target).replace(/^\//, '').split('/')) {
    if (segment === '..') segments.pop();
    else if (segment !== '.' && segment !== '') segments.push(segment);
  }
  return segments.join('/');
}

function safeDecode(name: string): string {
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}
