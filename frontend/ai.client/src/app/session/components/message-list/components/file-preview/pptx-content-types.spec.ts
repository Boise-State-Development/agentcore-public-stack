import { describe, it, expect } from 'vitest';
import JSZip from 'jszip';
import { dropOverridesForMissingParts } from './pptx-content-types';

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

describe('dropOverridesForMissingParts', () => {
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

    const repaired = await dropOverridesForMissingParts(await deck(types, parts));

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

    expect(await dropOverridesForMissingParts(data)).toBe(data);
  });

  it('matches part names case-insensitively and percent-decoded, as OPC does', async () => {
    const data = await deck(
      contentTypes([
        override('/PPT/Slides/Slide1.xml', SLIDE),
        override('/ppt/media/my%20image.xml', SLIDE),
      ]),
      ['ppt/slides/slide1.xml', 'ppt/media/my image.xml'],
    );

    expect(await dropOverridesForMissingParts(data)).toBe(data);
  });

  it('passes bytes that are not a ZIP through untouched', async () => {
    const data = new ArrayBuffer(16);

    expect(await dropOverridesForMissingParts(data)).toBe(data);
  });

  it('passes a ZIP with no content-types part through untouched', async () => {
    const zip = new JSZip();
    zip.file('ppt/slides/slide1.xml', '<x/>');
    const data = await zip.generateAsync({ type: 'arraybuffer' });

    expect(await dropOverridesForMissingParts(data)).toBe(data);
  });
});
