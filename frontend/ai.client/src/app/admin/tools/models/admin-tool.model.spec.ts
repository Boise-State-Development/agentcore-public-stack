import { describe, it, expect } from 'vitest';
import { detectAwsServiceFromUrl, extractAwsRegionFromUrl } from './admin-tool.model';

/**
 * Regression tests for CodeQL js/regex/missing-regexp-anchor (alerts #670/#671).
 *
 * AWS-endpoint detection must match the URL *host* only. Previously the regexes
 * ran unanchored against the whole URL string, so an AWS marker smuggled into a
 * path/query/fragment/userinfo component — or an AWS suffix used as a
 * non-terminal label of an attacker domain — was misclassified as an AWS host.
 * At signing time that would attach SigV4 IAM credentials to a request bound for
 * a non-AWS host.
 */
describe('admin-tool.model AWS URL detection (host-anchored)', () => {
  describe('legitimate AWS endpoints are still detected', () => {
    it('detects services', () => {
      expect(detectAwsServiceFromUrl('https://x.lambda-url.us-west-2.on.aws/mcp')).toBe('lambda');
      expect(detectAwsServiceFromUrl('https://x.execute-api.us-east-1.amazonaws.com/p')).toBe(
        'execute-api',
      );
      expect(
        detectAwsServiceFromUrl('https://g.bedrock-agentcore.eu-west-1.amazonaws.com/'),
      ).toBe('bedrock-agentcore');
    });

    it('extracts regions (including with an explicit port)', () => {
      expect(extractAwsRegionFromUrl('https://x.lambda-url.ap-south-1.on.aws/mcp')).toBe(
        'ap-south-1',
      );
      expect(
        extractAwsRegionFromUrl('https://x.execute-api.us-east-2.amazonaws.com:443/p'),
      ).toBe('us-east-2');
    });
  });

  describe('spoofed URLs must NOT be treated as AWS', () => {
    const spoofed: [string, string][] = [
      ['marker in query', 'https://evil.example/?x=.execute-api.us-east-1.amazonaws.com'],
      ['marker in path', 'https://evil.example/.lambda-url.us-east-1.on.aws/mcp'],
      [
        'aws suffix as non-terminal label',
        'https://x.execute-api.us-east-1.amazonaws.com.evil.example/mcp',
      ],
      ['marker in userinfo', 'https://.execute-api.us-east-1.amazonaws.com@evil.example/mcp'],
      ['marker in fragment', 'https://evil.example/#.bedrock-agentcore.us-east-1.amazonaws.com'],
    ];

    it.each(spoofed)('detectAwsServiceFromUrl returns "" for %s', (_desc, url) => {
      expect(detectAwsServiceFromUrl(url)).toBe('');
    });

    it.each(spoofed)('extractAwsRegionFromUrl returns "" for %s', (_desc, url) => {
      expect(extractAwsRegionFromUrl(url)).toBe('');
    });
  });

  describe('non-AWS / unparseable inputs', () => {
    it('returns "" for plain domains and empty input', () => {
      expect(detectAwsServiceFromUrl('https://mcp.example.com/mcp')).toBe('');
      expect(detectAwsServiceFromUrl('')).toBe('');
      expect(extractAwsRegionFromUrl('https://mcp.example.com/mcp')).toBe('');
      expect(extractAwsRegionFromUrl('not a url')).toBe('');
    });
  });
});
