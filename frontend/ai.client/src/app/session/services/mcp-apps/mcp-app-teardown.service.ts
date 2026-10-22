import { Injectable } from '@angular/core';
import type { McpAppBridge } from './mcp-app-bridge';

/**
 * Registry of live MCP App bridges, so the host can tell every open App
 * that it is about to go away.
 *
 * SEP-1865 requires the host to send `ui/resource-teardown` before tearing
 * a resource down "for any reason", and to wait for the response where it
 * can, so the App gets a chance to flush state to its own server. That
 * matters here more than it looks: the App's state is the App's own
 * responsibility to persist — the host deliberately isn't the store of
 * record — so a teardown the App never hears about is state nobody saves.
 *
 * The frame component already disposes its bridge on destroy, but by then
 * Angular is removing the iframe in the same tick and the App's window is
 * gone before it can react. This registry exists so teardown can be fired
 * at *navigation intent* instead — while the iframes are still alive and
 * their Views can still run code. The App's save call is proxied over HTTP
 * from the host page, so once its message reaches us the request outlives
 * the iframe.
 */
@Injectable({ providedIn: 'root' })
export class McpAppTeardownService {
  private readonly live = new Set<McpAppBridge>();

  /** Track a bridge; returns the deregistration callback. */
  register(bridge: McpAppBridge): () => void {
    this.live.add(bridge);
    return () => this.live.delete(bridge);
  }

  /** Number of live App bridges (specs + callers that want to skip work). */
  get liveCount(): number {
    return this.live.size;
  }

  /**
   * Notify every live App that it is being torn down, and resolve once they
   * have all acked or their grace windows have expired.
   *
   * Callers that can await (a route guard) get the spec's "wait for a
   * response" behavior. Callers that can't — a synchronous navigation
   * effect — should still call this WITHOUT awaiting: the notification goes
   * out while the iframes are alive, and each bridge keeps listening
   * through its own grace window, which is the part that actually rescues
   * the App's flush.
   */
  teardownAll(reason: string): Promise<void> {
    if (!this.live.size) return Promise.resolve();
    const bridges = [...this.live];
    this.live.clear();
    return Promise.all(
      bridges.map((bridge) => bridge.dispose(reason).catch(() => undefined)),
    ).then(() => undefined);
  }
}
