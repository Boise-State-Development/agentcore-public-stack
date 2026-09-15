import { describe, expect, it } from 'vitest';
import {
  downloadUrlFor,
  durableDownloadUrlFromHref,
  uploadIdFromHref,
} from './file-download-url';

const API = '/api';

/** A realistic presigned URL for a generated .docx in the user-files bucket. */
const SIGNED =
  'https://boisestateai-v2-user-file-uploads-897729136999.s3.us-west-2.amazonaws.com' +
  '/user-files/887113c0-70d1-7044-c305-191dbe255583/6b247682-b8fa-4797-95a5-ee06969380a7' +
  '/1a098460e41_ba6e8f342f1744e2/plan.docx' +
  '?response-content-type=application%2Fpdf&X-Amz-Algorithm=AWS4-HMAC-SHA256' +
  '&X-Amz-Signature=deadbeef';

/** The same URL after the model re-emitted it in prose — signature dropped. */
const TRUNCATED = SIGNED.split('?')[0];

describe('downloadUrlFor', () => {
  it('builds the durable app-api download path', () => {
    expect(downloadUrlFor(API, 'up1')).toBe('/api/files/up1/download');
  });

  it('encodes the upload id', () => {
    expect(downloadUrlFor(API, 'a/b')).toBe('/api/files/a%2Fb/download');
  });
});

describe('durableDownloadUrlFromHref', () => {
  it('rewrites a truncated user-files S3 link to the durable route', () => {
    expect(durableDownloadUrlFromHref(API, TRUNCATED)).toBe(
      '/api/files/1a098460e41_ba6e8f342f1744e2/download',
    );
  });

  it('rewrites a still-signed user-files link too (it expires within the hour)', () => {
    expect(durableDownloadUrlFromHref(API, SIGNED)).toBe(
      '/api/files/1a098460e41_ba6e8f342f1744e2/download',
    );
  });

  it('honours a cross-origin app-api base', () => {
    expect(durableDownloadUrlFromHref('http://localhost:8000', TRUNCATED)).toBe(
      'http://localhost:8000/files/1a098460e41_ba6e8f342f1744e2/download',
    );
  });

  it.each([
    ['an unrelated https link', 'https://docs.google.com/document/d/abc/edit'],
    ['an S3 URL that is not a user-files object key', 'https://bucket.s3.us-west-2.amazonaws.com/other/x.txt'],
    ['a user-files path on a non-S3 host', 'https://evil.example/user-files/u/s/up1/x.docx'],
    ['a non-absolute href', '/files/up1/download'],
    ['a javascript: URL', 'javascript:alert(1)'],
    ['garbage', 'not a url'],
  ])('leaves %s alone', (_label, href) => {
    expect(durableDownloadUrlFromHref(API, href)).toBeNull();
  });

  describe('uploadIdFromHref', () => {
    it('recovers the upload id from a signed user-files URL', () => {
      expect(uploadIdFromHref(SIGNED)).toBe(
        durableDownloadUrlFromHref(API, SIGNED)?.replace(
          `${API}/files/`,
          '',
        ).replace('/download', ''),
      );
      expect(uploadIdFromHref(SIGNED)).not.toBeNull();
    });

    it('returns null for anything that is not a user-files object URL', () => {
      expect(uploadIdFromHref('https://example.com/plan.docx')).toBeNull();
      expect(uploadIdFromHref('not a url')).toBeNull();
      expect(
        uploadIdFromHref(
          'https://bucket.s3.us-west-2.amazonaws.com/other/prefix/plan.docx',
        ),
      ).toBeNull();
    });
  });
});
