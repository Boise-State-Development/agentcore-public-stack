// Guards the invariant established by test-setup.ts: the JIT compiler must be
// available before spec code evaluates any raw (unlinked) Angular fesm chunk.
// Deliberately imports nothing from Angular statically — mirroring specs like
// app.spec.ts that only dynamic-import application code — so this fails
// deterministically if the setup file stops loading '@angular/compiler',
// instead of the suite failing flakily with "The injectable 'PlatformLocation'
// needs to be compiled using the JIT compiler".
import { describe, it, expect } from 'vitest';
import type { Type } from '@angular/core';

describe('test setup', () => {
  it('registers the JIT compiler facade before specs run', () => {
    const ng = (globalThis as { ng?: Record<string, unknown> }).ng;
    expect(ng?.['ɵcompilerFacade']).toBeDefined();
  });

  it('compiles a partial-declared (unlinked) injectable via the JIT fallback', async () => {
    const i0 = await import('@angular/core');
    const { TestBed } = await import('@angular/core/testing');
    const declare = i0 as unknown as Record<string, (d: object) => unknown>;
    class FakeUnlinked {
      static ɵfac = declare['ɵɵngDeclareFactory']({
        minVersion: '12.0.0',
        version: '21.2.17',
        ngImport: i0,
        type: FakeUnlinked,
        deps: [],
        target: (i0 as unknown as { ɵɵFactoryTarget: { Injectable: unknown } }).ɵɵFactoryTarget
          .Injectable,
      });
      static ɵprov = declare['ɵɵngDeclareInjectable']({
        minVersion: '12.0.0',
        version: '21.2.17',
        ngImport: i0,
        type: FakeUnlinked,
        providedIn: 'root',
      });
    }
    const instance = TestBed.inject(FakeUnlinked as unknown as Type<unknown>);
    expect(instance).toBeInstanceOf(FakeUnlinked);
  });
});
