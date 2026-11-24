import { describe, it, expect } from 'vitest';
import JSZip from 'jszip';
import { repairDeckForPreview } from './pptx-preview-repair';

const SLIDE_MASTER =
  'application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml';
const SLIDE = 'application/vnd.openxmlformats-officedocument.presentationml.slide+xml';

function override(partName: string, contentType: string): string {
  return `<Override PartName="${partName}" ContentType="${contentType}"/>`;
}

function contentTypes(overrides: string[]): string {
  return (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
    '<Default Extension="xml" ContentType="application/xml"/>' +
    overrides.join('') +
    '</Types>'
  );
}

/** A minimal package: the parts named, plus `[Content_Types].xml` as given. */
async function deck(types: string, parts: string[]): Promise<ArrayBuffer> {
  const zip = new JSZip();
  zip.file('[Content_Types].xml', types);
  for (const part of parts) zip.file(part, '<x/>');
  return zip.generateAsync({ type: 'arraybuffer' });
}

async function overridesIn(data: ArrayBuffer): Promise<string[]> {
  const xml = await (await JSZip.loadAsync(data)).file('[Content_Types].xml')!.async('string');
  return [...xml.matchAll(/PartName="([^"]+)"/g)].map((m) => m[1]);
}

describe('repairDeckForPreview', () => {
  it('drops the slide masters pptxgenjs lists but never writes', async () => {
    // What pptxgenjs writes for a three-slide deck: one slideMaster
    // override per slide, and only slideMaster1.xml in the archive.
    const types = contentTypes([
      override('/ppt/slideMasters/slideMaster1.xml', SLIDE_MASTER),
      override('/ppt/slides/slide1.xml', SLIDE),
      override('/ppt/slideMasters/slideMaster2.xml', SLIDE_MASTER),
      override('/ppt/slides/slide2.xml', SLIDE),
      override('/ppt/slideMasters/slideMaster3.xml', SLIDE_MASTER),
      override('/ppt/slides/slide3.xml', SLIDE),
    ]);
    const parts = [
      'ppt/slideMasters/slideMaster1.xml',
      'ppt/slides/slide1.xml',
      'ppt/slides/slide2.xml',
      'ppt/slides/slide3.xml',
    ];

    const repaired = await repairDeckForPreview(await deck(types, parts));

    expect(await overridesIn(repaired)).toEqual([
      '/ppt/slideMasters/slideMaster1.xml',
      '/ppt/slides/slide1.xml',
      '/ppt/slides/slide2.xml',
      '/ppt/slides/slide3.xml',
    ]);
    // Every part the deck really has is still there.
    const names = Object.keys((await JSZip.loadAsync(repaired)).files);
    for (const part of parts) expect(names).toContain(part);
  });

  it('returns a deck with nothing to drop as the same bytes, not a re-zip', async () => {
    const data = await deck(
      contentTypes([override('/ppt/slides/slide1.xml', SLIDE)]),
      ['ppt/slides/slide1.xml'],
    );

    expect(await repairDeckForPreview(data)).toBe(data);
  });

  it('matches part names case-insensitively and percent-decoded, as OPC does', async () => {
    const data = await deck(
      contentTypes([
        override('/PPT/Slides/Slide1.xml', SLIDE),
        override('/ppt/media/my%20image.xml', SLIDE),
      ]),
      ['ppt/slides/slide1.xml', 'ppt/media/my image.xml'],
    );

    expect(await repairDeckForPreview(data)).toBe(data);
  });

  it('passes bytes that are not a ZIP through untouched', async () => {
    const data = new ArrayBuffer(16);

    expect(await repairDeckForPreview(data)).toBe(data);
  });

  it('passes a ZIP with no content-types part through untouched', async () => {
    const zip = new JSZip();
    zip.file('ppt/slides/slide1.xml', '<x/>');
    const data = await zip.generateAsync({ type: 'arraybuffer' });

    expect(await repairDeckForPreview(data)).toBe(data);
  });
});

describe('repairDeckForPreview: layout background aliases', () => {
  const DEFAULT_MAP =
    'bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" ' +
    'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" ' +
    'hlink="hlink" folHlink="folHlink"';
  const LAYOUT = 'ppt/slideLayouts/slideLayout2.xml';

  function layoutXml(background: string, extra = ''): string {
    return (
      '<p:sldLayout xmlns:a="a" xmlns:p="p"><p:cSld name="Title Dark">' +
      `<p:bg><p:bgPr><a:solidFill>${background}</a:solidFill></p:bgPr></p:bg>` +
      '<p:spTree><p:sp><p:spPr><a:solidFill><a:schemeClr val="tx2"/></a:solidFill></p:spPr></p:sp></p:spTree>' +
      `</p:cSld>${extra}</p:sldLayout>`
    );
  }

  /** A layout whose rels point at slideMaster1, which carries `clrMap`. */
  async function layoutDeck(
    layout: string,
    clrMap = DEFAULT_MAP,
    target = '../slideMasters/slideMaster1.xml',
  ): Promise<ArrayBuffer> {
    const zip = new JSZip();
    zip.file('[Content_Types].xml', contentTypes([]));
    zip.file(LAYOUT, layout);
    zip.file(
      'ppt/slideLayouts/_rels/slideLayout2.xml.rels',
      '<Relationships xmlns="r"><Relationship Id="rId1" ' +
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" ' +
        `Target="${target}"/></Relationships>`,
    );
    zip.file('ppt/slideMasters/slideMaster1.xml', `<p:sldMaster><p:clrMap ${clrMap}/></p:sldMaster>`);
    return zip.generateAsync({ type: 'arraybuffer' });
  }

  async function layoutOf(data: ArrayBuffer): Promise<string> {
    return (await JSZip.loadAsync(data)).file(LAYOUT)!.async('string');
  }

  it("rewrites a background alias to the colour the master's map names", async () => {
    // pptxgenjs's dark layouts fill their background with tx2, which
    // pptx-preview leaves unmapped on a layout and so paints white.
    const repaired = await layoutOf(
      await repairDeckForPreview(await layoutDeck(layoutXml('<a:schemeClr val="tx2"/>'))),
    );

    expect(repaired).toContain('<p:bg><p:bgPr><a:solidFill><a:schemeClr val="dk2"/>');
    // Only the background: the shape's own tx2 is resolved by the library.
    expect(repaired).toContain('<p:spPr><a:solidFill><a:schemeClr val="tx2"/>');
  });

  it('keeps colour modifiers on the rewritten colour', async () => {
    const repaired = await layoutOf(
      await repairDeckForPreview(
        await layoutDeck(layoutXml('<a:schemeClr val="bg1"><a:lumMod val="95000"/></a:schemeClr>')),
      ),
    );

    expect(repaired).toContain('<a:schemeClr val="lt1"><a:lumMod val="95000"/></a:schemeClr>');
  });

  it("follows a master's non-default mapping", async () => {
    const swapped = DEFAULT_MAP.replace('tx2="dk2"', 'tx2="lt2"');
    const repaired = await layoutOf(
      await repairDeckForPreview(await layoutDeck(layoutXml('<a:schemeClr val="tx2"/>'), swapped)),
    );

    expect(repaired).toContain('<a:solidFill><a:schemeClr val="lt2"/></a:solidFill></p:bgPr>');
  });

  it("prefers the layout's own override mapping to its master's", async () => {
    const override = '<p:clrMapOvr><a:overrideClrMapping bg1="dk1" tx1="lt1" bg2="dk2" tx2="lt2"/></p:clrMapOvr>';
    const repaired = await layoutOf(
      await repairDeckForPreview(await layoutDeck(layoutXml('<a:schemeClr val="bg1"/>', override))),
    );

    expect(repaired).toContain('<a:solidFill><a:schemeClr val="dk1"/></a:solidFill></p:bgPr>');
  });

  it('returns the same bytes when the background holds no alias', async () => {
    const data = await layoutDeck(layoutXml('<a:schemeClr val="accent1"/>'));

    expect(await repairDeckForPreview(data)).toBe(data);
  });

  it('leaves a layout alone when its master cannot be found', async () => {
    const data = await layoutDeck(
      layoutXml('<a:schemeClr val="tx2"/>'),
      DEFAULT_MAP,
      '../slideMasters/slideMaster9.xml',
    );

    expect(await repairDeckForPreview(data)).toBe(data);
  });
});
