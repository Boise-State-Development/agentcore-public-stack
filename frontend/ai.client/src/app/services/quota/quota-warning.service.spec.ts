import { TestBed } from '@angular/core/testing';
import {
  QuotaWarningService,
  QuotaWarning,
  QuotaExceeded,
  QuotaSessionNotice,
} from './quota-warning.service';

function makeSessionNotice(
  overrides: Partial<QuotaSessionNotice> = {},
): QuotaSessionNotice {
  return {
    type: 'quota_session_notice',
    sessionId: 'session-1',
    sessionCost: 7.58,
    quotaLimit: 30,
    sessionPercentageOfLimit: 25.3,
    thresholdPercentage: 25,
    message: 'This conversation has used $7.58 of your $30.00 monthly quota.',
    ...overrides,
  };
}

describe('QuotaWarningService', () => {
  let service: QuotaWarningService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(QuotaWarningService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('setWarning', () => {
    it('should set warning and update signals', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      service.setWarning(warning);

      expect(service.activeWarning()).toEqual(warning);
      expect(service.hasVisibleWarning()).toBe(true);
    });

    it('should not update if same warning level and usage', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      service.setWarning(warning);
      const firstWarning = service.activeWarning();
      
      service.setWarning(warning);
      
      expect(service.activeWarning()).toBe(firstWarning);
    });

    it('should update if different warning level', () => {
      const warning1: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      const warning2: QuotaWarning = {
        ...warning1,
        warningLevel: '90%',
        percentageUsed: 90
      };

      service.setWarning(warning1);
      service.setWarning(warning2);

      expect(service.activeWarning()).toEqual(warning2);
    });
  });

  describe('dismissWarning', () => {
    it('should hide visible warning', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      service.setWarning(warning);
      expect(service.hasVisibleWarning()).toBe(true);

      service.dismissWarning();
      expect(service.hasVisibleWarning()).toBe(false);
    });
  });

  describe('clearWarning', () => {
    it('should clear all warning state', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      service.setWarning(warning);
      service.clearWarning();

      expect(service.activeWarning()).toBeNull();
      expect(service.hasVisibleWarning()).toBe(false);
    });
  });

  describe('setQuotaExceeded', () => {
    it('should set quota exceeded and clear active warning', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      const exceeded: QuotaExceeded = {
        type: 'quota_exceeded',
        currentUsage: 12,
        quotaLimit: 10,
        percentageUsed: 120,
        periodType: 'monthly',
        resetInfo: 'Resets on 1st',
        message: 'Quota exceeded'
      };

      service.setWarning(warning);
      service.setQuotaExceeded(exceeded);

      expect(service.quotaExceeded()).toEqual(exceeded);
      expect(service.activeWarning()).toBeNull();
      expect(service.isQuotaExceeded()).toBe(true);
    });
  });

  describe('clearQuotaExceeded', () => {
    it('should clear quota exceeded state', () => {
      const exceeded: QuotaExceeded = {
        type: 'quota_exceeded',
        currentUsage: 12,
        quotaLimit: 10,
        percentageUsed: 120,
        periodType: 'monthly',
        resetInfo: 'Resets on 1st',
        message: 'Quota exceeded'
      };

      service.setQuotaExceeded(exceeded);
      service.clearQuotaExceeded();

      expect(service.quotaExceeded()).toBeNull();
      expect(service.isQuotaExceeded()).toBe(false);
    });
  });

  describe('clearAll', () => {
    it('should clear all state', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      const exceeded: QuotaExceeded = {
        type: 'quota_exceeded',
        currentUsage: 12,
        quotaLimit: 10,
        percentageUsed: 120,
        periodType: 'monthly',
        resetInfo: 'Resets on 1st',
        message: 'Quota exceeded'
      };

      service.setWarning(warning);
      service.setQuotaExceeded(exceeded);
      service.setSessionNotice(makeSessionNotice());
      service.clearAll();

      expect(service.activeWarning()).toBeNull();
      expect(service.quotaExceeded()).toBeNull();
      expect(service.sessionNotice()).toBeNull();
    });
  });

  describe('setSessionNotice', () => {
    it('should set the notice and make it visible', () => {
      const notice = makeSessionNotice();

      service.setSessionNotice(notice);

      expect(service.sessionNotice()).toEqual(notice);
      expect(service.hasVisibleSessionNotice()).toBe(true);
      expect(service.formattedSessionUsage()).toBe('$7.58 of $30.00');
    });

    it('should keep a dismissal until the cost moves', () => {
      service.setSessionNotice(makeSessionNotice());
      service.dismissSessionNotice();
      expect(service.hasVisibleSessionNotice()).toBe(false);

      // Same conversation, same cost — the backend re-emits every turn while
      // over the share, and that must not undo the user's dismissal.
      service.setSessionNotice(makeSessionNotice());
      expect(service.hasVisibleSessionNotice()).toBe(false);

      // A more expensive turn is new information, so it comes back.
      service.setSessionNotice(makeSessionNotice({ sessionCost: 9.12 }));
      expect(service.hasVisibleSessionNotice()).toBe(true);
    });

    it('should resurface for a different conversation', () => {
      service.setSessionNotice(makeSessionNotice());
      service.dismissSessionNotice();

      service.setSessionNotice(makeSessionNotice({ sessionId: 'session-2' }));

      expect(service.hasVisibleSessionNotice()).toBe(true);
    });

    it('should yield to the quota-exceeded state', () => {
      service.setSessionNotice(makeSessionNotice());
      service.setQuotaExceeded({
        type: 'quota_exceeded',
        currentUsage: 30,
        quotaLimit: 30,
        percentageUsed: 100,
        periodType: 'monthly',
        resetInfo: 'Resets on 1st',
        message: 'Quota exceeded'
      });

      expect(service.hasVisibleSessionNotice()).toBe(false);
    });

    it('should return empty formatted usage with no notice', () => {
      expect(service.formattedSessionUsage()).toBe('');
    });
  });

  describe('resetDismissed', () => {
    it('should reset dismissed state', () => {
      const warning: QuotaWarning = {
        type: 'quota_warning',
        warningLevel: '80%',
        currentUsage: 8,
        quotaLimit: 10,
        percentageUsed: 80,
        remaining: 2,
        message: 'Warning message'
      };

      service.setWarning(warning);
      service.dismissWarning();
      expect(service.hasVisibleWarning()).toBe(false);

      service.resetDismissed();
      expect(service.hasVisibleWarning()).toBe(true);
    });
  });

  describe('computed signals', () => {
    describe('severity', () => {
      it('should return "exceeded" for quota exceeded', () => {
        const exceeded: QuotaExceeded = {
          type: 'quota_exceeded',
          currentUsage: 12,
          quotaLimit: 10,
          percentageUsed: 120,
          periodType: 'monthly',
          resetInfo: 'Resets on 1st',
          message: 'Quota exceeded'
        };

        service.setQuotaExceeded(exceeded);
        expect(service.severity()).toBe('exceeded');
      });

      it('should return "critical" for 90%+ usage', () => {
        const warning: QuotaWarning = {
          type: 'quota_warning',
          warningLevel: '90%',
          currentUsage: 9,
          quotaLimit: 10,
          percentageUsed: 90,
          remaining: 1,
          message: 'Warning message'
        };

        service.setWarning(warning);
        expect(service.severity()).toBe('critical');
      });

      it('should return "warning" for <90% usage', () => {
        const warning: QuotaWarning = {
          type: 'quota_warning',
          warningLevel: '80%',
          currentUsage: 8,
          quotaLimit: 10,
          percentageUsed: 80,
          remaining: 2,
          message: 'Warning message'
        };

        service.setWarning(warning);
        expect(service.severity()).toBe('warning');
      });

      it('should return null when no warning', () => {
        expect(service.severity()).toBeNull();
      });
    });

    describe('formattedUsage', () => {
      it('should format quota exceeded usage', () => {
        const exceeded: QuotaExceeded = {
          type: 'quota_exceeded',
          currentUsage: 12.50,
          quotaLimit: 10.00,
          percentageUsed: 125,
          periodType: 'monthly',
          resetInfo: 'Resets on 1st',
          message: 'Quota exceeded'
        };

        service.setQuotaExceeded(exceeded);
        expect(service.formattedUsage()).toBe('$12.50 / $10.00');
      });

      it('should format warning usage', () => {
        const warning: QuotaWarning = {
          type: 'quota_warning',
          warningLevel: '80%',
          currentUsage: 8.75,
          quotaLimit: 10.00,
          percentageUsed: 87.5,
          remaining: 1.25,
          message: 'Warning message'
        };

        service.setWarning(warning);
        expect(service.formattedUsage()).toBe('$8.75 / $10.00');
      });

      it('should return empty string when no warning', () => {
        expect(service.formattedUsage()).toBe('');
      });
    });

    describe('formattedRemaining', () => {
      it('should format remaining amount', () => {
        const warning: QuotaWarning = {
          type: 'quota_warning',
          warningLevel: '80%',
          currentUsage: 8,
          quotaLimit: 10,
          percentageUsed: 80,
          remaining: 2.50,
          message: 'Warning message'
        };

        service.setWarning(warning);
        expect(service.formattedRemaining()).toBe('$2.50');
      });

      it('should return empty string when no warning', () => {
        expect(service.formattedRemaining()).toBe('');
      });
    });

    describe('resetInfo', () => {
      it('should return reset info for quota exceeded', () => {
        const exceeded: QuotaExceeded = {
          type: 'quota_exceeded',
          currentUsage: 12,
          quotaLimit: 10,
          percentageUsed: 120,
          periodType: 'monthly',
          resetInfo: 'Resets on January 1st',
          message: 'Quota exceeded'
        };

        service.setQuotaExceeded(exceeded);
        expect(service.resetInfo()).toBe('Resets on January 1st');
      });

      it('should return empty string when no quota exceeded', () => {
        expect(service.resetInfo()).toBe('');
      });
    });
  });
});