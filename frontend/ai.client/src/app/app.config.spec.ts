import { APP_INITIALIZER } from '@angular/core';
import { SessionService } from './auth/session.service';
import { appConfig } from './app.config';

/**
 * The chained ConfigService→SessionService init was retired with config.json.
 * APP_INITIALIZER now boots the BFF cookie session only.
 */
function getInitializerProvider(): any {
  const providers = appConfig.providers || [];
  return providers.find((p: any) => p.provide === APP_INITIALIZER);
}

describe('APP_INITIALIZER', () => {
  it('registers a multi-provider for APP_INITIALIZER', () => {
    const provider = getInitializerProvider();
    expect(provider).toBeDefined();
    expect(provider.provide).toBe(APP_INITIALIZER);
    expect(provider.multi).toBe(true);
  });

  it('depends on SessionService only (ConfigService is no longer chained)', () => {
    const provider = getInitializerProvider();
    expect(provider.deps).toEqual([SessionService]);
  });

  it('factory invokes sessionService.bootstrap() and returns its promise', async () => {
    const provider = getInitializerProvider();
    const mockSession = { bootstrap: vi.fn().mockResolvedValue(undefined) } as any;

    const initializer = provider.useFactory(mockSession);
    const result = initializer();

    expect(mockSession.bootstrap).toHaveBeenCalledTimes(1);
    await expect(result).resolves.toBeUndefined();
  });

  it('propagates a bootstrap rejection so Angular surfaces it', async () => {
    const provider = getInitializerProvider();
    const mockSession = {
      bootstrap: vi.fn().mockRejectedValue(new Error('bootstrap failed')),
    } as any;

    const initializer = provider.useFactory(mockSession);
    await expect(initializer()).rejects.toThrow('bootstrap failed');
  });
});
