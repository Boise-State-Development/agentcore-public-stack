import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { IncreaseRequestRecord } from '../models';
import { AwsLimitsStateService } from '../services/aws-limits-state.service';
import { IncreaseHistoryDialogComponent } from './increase-history-dialog.component';

function request(id: string, status: string): IncreaseRequestRecord {
  return {
    requestId: id, serviceCode: 'lambda', serviceName: 'AWS Lambda', quotaCode: 'L-1', quotaName: `Quota ${id} item`,
    desiredValue: 2000, status, requestType: null, caseId: null, created: '2026-11-01T00:00:00Z',
    lastUpdated: null, unit: null, requestedBy: null, consoleUrl: 'https://console',
  };
}

describe('IncreaseHistoryDialogComponent', () => {
  let state: { history: ReturnType<typeof signal<IncreaseRequestRecord[]>>; loadHistory: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();
    state = {
      history: signal([request('open', 'PENDING'), request('done', 'APPROVED'), request('no', 'DENIED')]),
      loadHistory: vi.fn().mockResolvedValue(undefined),
    };
    TestBed.configureTestingModule({
      providers: [
        {
          provide: AwsLimitsStateService,
          useValue: {
            ...state,
            historyLoading: signal(false),
            historyError: signal(null),
            dashboard: signal({ guidanceUrl: 'https://docs' }),
          },
        },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open() {
    const opened = await openInCdkDialog<IncreaseHistoryDialogComponent, unknown, void>(IncreaseHistoryDialogComponent, {});
    TestBed.tick();
    return opened;
  }

  const rows = (container: HTMLElement) =>
    [...container.querySelectorAll('tbody tr')].map(r => r.textContent?.match(/Quota (\w+) item/)?.[1]);

  it('reads the history when it opens, and hides approved requests by default', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Increase requests' });
    expect(state.loadHistory).toHaveBeenCalled();
    expect((container.querySelector('#aws-limits-hide-approved') as HTMLInputElement).checked).toBe(true);
    expect(rows(container)).toEqual(['open', 'no']);
    expect(container.querySelector('[data-testid="hidden-count"]')?.textContent).toContain('1 approved request hidden');
  });

  it('shows approved requests when the box is cleared', async () => {
    const { container } = await open();
    (container.querySelector('#aws-limits-hide-approved') as HTMLInputElement).click();
    TestBed.tick();
    expect(rows(container)).toEqual(['open', 'done', 'no']);
  });
});
