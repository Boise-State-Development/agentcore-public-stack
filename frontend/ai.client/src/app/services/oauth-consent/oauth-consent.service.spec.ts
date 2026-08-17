import { TestBed } from '@angular/core/testing';
import { OAuthConsentService } from './oauth-consent.service';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { SessionService } from '../../session/services/session/session.service';

describe('OAuthConsentService', () => {
  let service: OAuthConsentService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: UserConnectorsService, useValue: {} },
        {
          provide: SessionService,
          useValue: { dismissPendingInterrupt: () => Promise.resolve() },
        },
      ],
    });
    service = TestBed.inject(OAuthConsentService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('requestConsent dedup by interruptId', () => {
    it('drops a re-emission with the same interruptId', () => {
      service.requestConsent(
        'google',
        'https://accounts.example/consent?req=1',
        'i-1',
        'msg-1',
        'sess-1',
      );
      service.requestConsent(
        'google',
        'https://accounts.example/consent?req=2',
        'i-1',
        'msg-1',
        'sess-1',
      );

      const pending = service.pending();
      expect(pending.length).toBe(1);
      // First emission's URL is preserved — the duplicate did not overwrite.
      expect(pending[0].authorizationUrl).toBe('https://accounts.example/consent?req=1');
    });

    it('surfaces a new interruptId for the same provider', () => {
      service.requestConsent('google', 'https://accounts.example/c1', 'i-1');
      service.requestConsent('google', 'https://accounts.example/c2', 'i-2');

      // Provider-keyed map still collapses to one entry, but the second
      // call refreshed the interruptId — it was not dropped as a duplicate.
      const pending = service.pending();
      expect(pending.length).toBe(1);
      expect(pending[0].interruptId).toBe('i-2');
      expect(pending[0].authorizationUrl).toBe('https://accounts.example/c2');
    });

    it('surfaces distinct providers independently', () => {
      service.requestConsent('google', 'https://accounts.example/g', 'i-g');
      service.requestConsent('slack', 'https://slack.example/s', 'i-s');

      expect(service.pending().length).toBe(2);
    });

    it('keeps interruptId-less requests (settings-page consent) unduped', () => {
      service.requestConsent('google', 'https://accounts.example/c1');
      service.requestConsent('google', 'https://accounts.example/c2');

      // No interruptId means no dedup key — the second call still refreshes
      // the entry. Settings-page flows have no agent turn to resume.
      expect(service.pending().length).toBe(1);
      expect(service.pending()[0].authorizationUrl).toBe('https://accounts.example/c2');
    });

    it('drops a re-emission even after the request was dismissed', () => {
      service.requestConsent('google', 'https://accounts.example/c', 'i-1');
      service.dismiss('google', { syncServer: false });
      expect(service.pending().length).toBe(0);

      // Stream replay or late breadcrumb resurrection of the same id —
      // the prompt must not come back.
      service.requestConsent('google', 'https://accounts.example/c', 'i-1');
      expect(service.pending().length).toBe(0);
    });

    it('clear() resets the dedup set so a fresh session can re-prompt', () => {
      service.requestConsent('google', 'https://accounts.example/c', 'i-1');
      service.clear();

      service.requestConsent('google', 'https://accounts.example/c', 'i-1');
      expect(service.pending().length).toBe(1);
    });
  });

  describe('pre-flight consents (no interruptId)', () => {
    // Emitted when an OAuth-gated MCP server refused `tools/list`, so the
    // tool never registered and no turn is paused. The backend re-emits
    // these every turn that rebuilds the agent, so dismissal has to stick
    // locally — there is no server-side breadcrumb to DELETE.
    const URL_A = 'https://accounts.example/consent?a=1';

    it('surfaces a request with no interruptId', () => {
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-1', 'sess-1');

      const pending = service.pending();
      expect(pending.length).toBe(1);
      expect(pending[0].providerId).toBe('github-oauth');
      expect(pending[0].interruptId).toBeUndefined();
    });

    it('dedupes by providerId while the prompt is live', () => {
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-1', 'sess-1');
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-2', 'sess-1');

      expect(service.pending().length).toBe(1);
    });

    it('does not resurrect after the user dismisses it', () => {
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-1', 'sess-1');
      service.dismiss('github-oauth');
      expect(service.pending().length).toBe(0);

      // Next turn re-emits because consent still has not landed.
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-2', 'sess-1');
      expect(service.pending().length).toBe(0);
    });

    it('still surfaces an interrupt-driven prompt after a pre-flight dismissal', () => {
      // A paused turn is blocking on consent — the user must be able to act
      // even though they dismissed the passive pre-flight nudge earlier.
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-1', 'sess-1');
      service.dismiss('github-oauth');

      service.requestConsent('github-oauth', URL_A, 'i-99', 'msg-2', 'sess-1');
      expect(service.pending().length).toBe(1);
      expect(service.pending()[0].interruptId).toBe('i-99');
    });

    it('clear() lifts the dismissal', () => {
      service.requestConsent('github-oauth', URL_A, undefined, 'msg-1', 'sess-1');
      service.dismiss('github-oauth');
      service.clear();

      service.requestConsent('github-oauth', URL_A, undefined, 'msg-2', 'sess-2');
      expect(service.pending().length).toBe(1);
    });
  });
});
