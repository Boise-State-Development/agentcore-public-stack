import { describe, it, expect, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Component } from '@angular/core';
import { Router, provideRouter } from '@angular/router';
import { provideLocationMocks } from '@angular/common/testing';
import {
  LEGACY_MIGRATION_HOST,
  isLegacyMigrationHost,
  legacyMigrationHostGuard,
} from './legacy-migration-host';

/**
 * ⚠️ TEMPORARY, with the surface it gates — delete this file together with
 * `legacy-migration-host.ts`, the `/assistants` route and `agents/migration/`.
 *
 * The `/assistants` explainer is scoped to the production apex because the question it now
 * answers ("where did the assistants I built on the previous site go?") only exists there.
 * Two things must agree for that to hold: the sidenav entry (asserted in `sidenav.spec.ts`)
 * and the route guard. A nav entry that hides while the URL still renders is not a scope,
 * it is a hidden page — so the guard's redirect is asserted here rather than assumed.
 */
describe('legacy migration host gate', () => {
  describe('isLegacyMigrationHost', () => {
    it('accepts the production apex, with or without www', () => {
      expect(isLegacyMigrationHost('boisestate.ai')).toBe(true);
      expect(isLegacyMigrationHost('www.boisestate.ai')).toBe(true);
    });

    it('is case-insensitive, because hostnames are', () => {
      expect(isLegacyMigrationHost('BoiseState.AI')).toBe(true);
    });

    it('rejects the deployed non-production environments', () => {
      // Exact match, not a suffix test: `dev.` and `beta.` are inside the same domain and
      // a naive `endsWith('boisestate.ai')` would let both through.
      expect(isLegacyMigrationHost('dev.boisestate.ai')).toBe(false);
      expect(isLegacyMigrationHost('beta.boisestate.ai')).toBe(false);
    });

    it('accepts localhost so the copy can be read in place while it is worked on', () => {
      // Not an audience — the bench. Deliberately separate from the case above so that
      // deleting it never reads as "and dev/beta too".
      expect(isLegacyMigrationHost('localhost')).toBe(true);
    });

    it('rejects a lookalike host that merely ends in the apex', () => {
      expect(isLegacyMigrationHost('notboisestate.ai')).toBe(false);
      expect(isLegacyMigrationHost('boisestate.ai.example.com')).toBe(false);
    });

    it('treats a missing hostname as off-host', () => {
      expect(isLegacyMigrationHost(undefined)).toBe(false);
      expect(isLegacyMigrationHost('')).toBe(false);
    });
  });

  describe('legacyMigrationHostGuard', () => {
    @Component({ template: '' })
    class BlankComponent {}

    function configure(onHost: boolean) {
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        providers: [
          provideRouter([
            {
              path: 'assistants',
              component: BlankComponent,
              canActivate: [legacyMigrationHostGuard],
              pathMatch: 'full',
            },
            { path: 'agents', component: BlankComponent },
          ]),
          provideLocationMocks(),
          { provide: LEGACY_MIGRATION_HOST, useValue: onHost },
        ],
      });
      return TestBed.inject(Router);
    }

    afterEach(() => {
      TestBed.resetTestingModule();
    });

    it('lets the explainer render on the production apex', async () => {
      const router = configure(true);
      await router.navigateByUrl('/assistants');
      expect(router.url).toBe('/assistants');
    });

    it('restores the silent redirect onto /agents everywhere else', async () => {
      // Not a blocked navigation: off-host, `/assistants` is just an old alias, and
      // returning `false` would strand the user on a dead URL instead.
      const router = configure(false);
      await router.navigateByUrl('/assistants');
      expect(router.url).toBe('/agents');
    });
  });
});
