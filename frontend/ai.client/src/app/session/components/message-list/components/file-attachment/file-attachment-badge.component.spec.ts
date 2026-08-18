import { describe, it, expect } from 'vitest';
import {
  FILE_TYPE_STYLES,
  DEFAULT_STYLE,
} from './file-attachment-badge.component';
import { ALLOWED_MIME_TYPES } from '../../../../../services/file-upload';

/**
 * Every uploadable type needs its own card style.
 *
 * A type missing from FILE_TYPE_STYLES doesn't fail loudly — it silently
 * falls through to DEFAULT_STYLE and the card renders a generic grey "FILE"
 * chip. That is exactly how .pptx shipped: the upload allowlist gained the
 * type but this map didn't, so decks arrived looking like anonymous blobs.
 *
 * Pinning the map against the upload allowlist means the next type added to
 * one has to be added to the other.
 */
describe('file attachment card styles', () => {
  const PPTX_MIME =
    'application/vnd.openxmlformats-officedocument.presentationml.presentation';

  it('gives .pptx its own style rather than the generic fallback', () => {
    const style = FILE_TYPE_STYLES[PPTX_MIME];
    expect(style).toBeDefined();
    expect(style.label).toBe('PPTX');
    expect(style.label).not.toBe(DEFAULT_STYLE.label);
  });

  it('uses a presentation icon for .pptx, not a plain document', () => {
    expect(FILE_TYPE_STYLES[PPTX_MIME].icon).toBe('heroPresentationChartBar');
    expect(FILE_TYPE_STYLES[PPTX_MIME].icon).not.toBe(DEFAULT_STYLE.icon);
  });

  it('styles every mime type the upload allowlist accepts', () => {
    const unstyled = Object.keys(ALLOWED_MIME_TYPES).filter(
      (mime) => !(mime in FILE_TYPE_STYLES),
    );
    expect(unstyled).toEqual([]);
  });
});
