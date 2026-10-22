import { describe, it, expect, beforeEach, vi } from 'vitest';
import { McpAppTeardownService } from './mcp-app-teardown.service';
import type { McpAppBridge } from './mcp-app-bridge';

/** Only `dispose` is exercised; the registry never touches anything else. */
function fakeBridge(dispose = vi.fn().mockResolvedValue(undefined)) {
  return { dispose } as unknown as McpAppBridge & {
    dispose: ReturnType<typeof vi.fn>;
  };
}

describe('McpAppTeardownService', () => {
  let svc: McpAppTeardownService;

  beforeEach(() => {
    svc = new McpAppTeardownService();
  });

  it('tears down every live App with the given reason', async () => {
    const a = fakeBridge();
    const b = fakeBridge();
    svc.register(a);
    svc.register(b);

    await svc.teardownAll('conversation-change');

    expect(a.dispose).toHaveBeenCalledWith('conversation-change');
    expect(b.dispose).toHaveBeenCalledWith('conversation-change');
  });

  it('clears the registry so a second navigation re-notifies nothing', async () => {
    const a = fakeBridge();
    svc.register(a);

    await svc.teardownAll('first');
    await svc.teardownAll('second');

    expect(a.dispose).toHaveBeenCalledTimes(1);
    expect(svc.liveCount).toBe(0);
  });

  it('deregisters a frame that unmounts on its own', async () => {
    const a = fakeBridge();
    const unregister = svc.register(a);
    unregister();

    await svc.teardownAll('conversation-change');

    expect(a.dispose).not.toHaveBeenCalled();
    expect(svc.liveCount).toBe(0);
  });

  it('one hung App does not strand the others', async () => {
    const rejecting = fakeBridge(vi.fn().mockRejectedValue(new Error('gone')));
    const healthy = fakeBridge();
    svc.register(rejecting);
    svc.register(healthy);

    // Navigation waits on this; a failed teardown must not reject it.
    await expect(svc.teardownAll('conversation-change')).resolves.toBeUndefined();
    expect(healthy.dispose).toHaveBeenCalled();
  });

  it('resolves immediately when no App is open', async () => {
    await expect(svc.teardownAll('conversation-change')).resolves.toBeUndefined();
    expect(svc.liveCount).toBe(0);
  });
});
