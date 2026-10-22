import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
  TestRequest,
} from '@angular/common/http/testing';

import { AdminMarketplaceService } from './admin-marketplace.service';
import { ConfigService } from '../../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';

/**
 * The review flow opts out of the global error toast.
 *
 * Every failure on these calls already renders inline, next to the control that caused it.
 * Without the opt-out an admin whose Approve is refused reads the backend's message twice —
 * once where they are looking, once in a corner toast that is further away and disappears
 * on its own.
 *
 * Asserted per call rather than as "the service suppresses toasts", because the rule is
 * *not* service-wide: silencing a call whose caller renders nothing would turn a visible
 * failure into a silent one. A new call has to earn the opt-out by having inline UI, and
 * the list below is the record of which ones have.
 */
describe('AdminMarketplaceService — error surfacing', () => {
  let service: AdminMarketplaceService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AdminMarketplaceService,
        { provide: ConfigService, useValue: { appApiUrl: () => 'https://api.test' } },
      ],
    });
    service = TestBed.inject(AdminMarketplaceService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  function suppressed(req: TestRequest): boolean {
    return req.request.context.get(SUPPRESS_ERROR_TOAST);
  }

  it('suppresses the toast when reading a submission', () => {
    void service.loadSubmission('ast-001').catch(() => undefined);
    const req = http.expectOne('https://api.test/admin/agents/ast-001/submission');
    expect(suppressed(req)).toBe(true);
    req.flush({});
  });

  it('suppresses the toast when reading the diff', () => {
    // The backend distinguishes "predates snapshots" from a transport failure, and the
    // diff component renders that distinction. A toast would flatten it.
    void service.loadDiff('ast-001').catch(() => undefined);
    const req = http.expectOne('https://api.test/admin/agents/ast-001/diff');
    expect(suppressed(req)).toBe(true);
    req.flush({});
  });

  it('suppresses the toast when recording a decision', () => {
    // The case that prompted this: Approve refused on a private agent showed the same
    // message inline and in a toast.
    void service.review('ast-001', { decision: 'approve' }).catch(() => undefined);
    const req = http.expectOne('https://api.test/admin/agents/ast-001/review');
    expect(suppressed(req)).toBe(true);
    req.flush({});
  });

  it('suppresses the toast when deciding a withdrawal', () => {
    void service
      .decideWithdrawal('ast-001', { decision: 'grant' })
      .catch(() => undefined);
    const req = http.expectOne('https://api.test/admin/agents/ast-001/withdrawal');
    expect(suppressed(req)).toBe(true);
    req.flush({});
  });

  it('leaves the toast on for calls with no inline error surface', () => {
    // The guard against a well-meaning sweep that suppresses everything. A takedown
    // failure has no inline region on the Listings page, so the toast is its only
    // surface — silencing it would turn a visible failure into a silent one.
    void service.takedown('ast-001', 'Because.').catch(() => undefined);
    const req = http.expectOne('https://api.test/admin/agents/ast-001/takedown');
    expect(suppressed(req)).toBe(false);
    req.flush({});
  });
});
