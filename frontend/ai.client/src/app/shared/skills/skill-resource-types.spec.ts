/**
 * Guards the client mirror of the skill-resource type allowlist.
 *
 * The server is the real control, but this mirror decides what the file picker
 * offers and what the forms refuse locally — so it must never drift open on the
 * dangerous extensions. The stored-XSS chain these tests exist for: a
 * `.html` skill resource served from the SPA's own origin executed its inline
 * `<script>` in a reading admin's authenticated session.
 */
import {
  ALLOWED_RESOURCE_EXTENSIONS,
  RESOURCE_ACCEPT_ATTR,
  isAllowedResourceFilename,
} from './skill-resource-types';

describe('skill resource type allowlist (client mirror)', () => {
  const SCRIPTABLE = [
    'html',
    'htm',
    'xhtml',
    'xht',
    'xml',
    'svg',
    'shtml',
    'mhtml',
    'swf',
  ];

  it('excludes every scriptable document extension', () => {
    for (const ext of SCRIPTABLE) {
      expect(ALLOWED_RESOURCE_EXTENSIONS).not.toContain(ext);
      expect(isAllowedResourceFilename(`payload.${ext}`)).toBe(false);
    }
  });

  it('rejects a scriptable extension regardless of case', () => {
    expect(isAllowedResourceFilename('payload.HTML')).toBe(false);
    expect(isAllowedResourceFilename('payload.SvG')).toBe(false);
  });

  it('resolves on the last extension, not the first', () => {
    // `notes.md.html` is an HTML file; matching on `.md` would wave it through.
    expect(isAllowedResourceFilename('notes.md.html')).toBe(false);
    expect(isAllowedResourceFilename('notes.html.md')).toBe(true);
  });

  it('accepts the ordinary bundle file types', () => {
    for (const name of ['forms.md', 'data.json', 'chart.png', 'manual.pdf']) {
      expect(isAllowedResourceFilename(name)).toBe(true);
    }
  });

  it('rejects an empty or extension-less name', () => {
    expect(isAllowedResourceFilename('')).toBe(false);
    expect(isAllowedResourceFilename('payload')).toBe(false);
  });

  it('builds a dotted accept attribute with no scriptable type', () => {
    expect(RESOURCE_ACCEPT_ATTR).toContain('.md');
    for (const ext of SCRIPTABLE) {
      expect(RESOURCE_ACCEPT_ATTR.split(',')).not.toContain(`.${ext}`);
    }
  });
});
