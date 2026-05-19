import { describe, it, expect, beforeEach } from 'vitest';
import { McpAppConsentService } from './mcp-app-consent.service';

describe('McpAppConsentService', () => {
  let svc: McpAppConsentService;

  beforeEach(() => {
    svc = new McpAppConsentService();
  });

  it('surfaces a pending prompt and resolves it on answer(true)', async () => {
    const { id, granted } = svc.request({
      kind: 'open-link',
      url: 'https://x.test',
    });
    expect(svc.pending()).toHaveLength(1);
    const entry = svc.pending()[0];
    expect(entry.id).toBe(id);
    expect(entry.request).toEqual({ kind: 'open-link', url: 'https://x.test' });

    svc.answer(entry.id, true);
    await expect(granted).resolves.toBe(true);
    expect(svc.pending()).toHaveLength(0);
  });

  it('resolves false on deny', async () => {
    const { granted } = svc.request({
      kind: 'capabilities',
      capabilities: ['microphone'],
    });
    svc.answer(svc.pending()[0].id, false);
    await expect(granted).resolves.toBe(false);
  });

  it('answer() on an unknown id is a no-op', () => {
    svc.request({ kind: 'open-link', url: 'https://x.test' });
    svc.answer('nope', true);
    expect(svc.pending()).toHaveLength(1);
  });

  it('reset() fails open prompts closed (resolve false) and clears', async () => {
    const a = svc.request({ kind: 'open-link', url: 'https://a.test' });
    const b = svc.request({ kind: 'open-link', url: 'https://b.test' });
    expect(svc.pending()).toHaveLength(2);

    svc.reset();
    expect(svc.pending()).toHaveLength(0);
    await expect(a.granted).resolves.toBe(false);
    await expect(b.granted).resolves.toBe(false);
  });

  it('keeps multiple prompts independently addressable', async () => {
    const a = svc.request({ kind: 'open-link', url: 'https://a.test' });
    const b = svc.request({ kind: 'open-link', url: 'https://b.test' });
    const [ea, eb] = svc.pending();

    svc.answer(eb.id, true);
    await expect(b.granted).resolves.toBe(true);
    expect(svc.pending()).toHaveLength(1);
    expect(svc.pending()[0].id).toBe(ea.id);

    svc.answer(ea.id, false);
    await expect(a.granted).resolves.toBe(false);
  });
});
