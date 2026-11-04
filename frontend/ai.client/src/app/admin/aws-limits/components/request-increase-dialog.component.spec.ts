import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AwsLimit } from '../models';
import { AwsLimitsHttpService } from '../services/aws-limits-http.service';
import {
  RequestIncreaseDialogComponent,
  RequestIncreaseDialogData,
  RequestIncreaseDialogResult,
} from './request-increase-dialog.component';

const LIMIT: AwsLimit = {
  serviceCode: 'lambda',
  serviceName: 'AWS Lambda',
  quotaCode: 'L-B99A9384',
  quotaName: 'Concurrent executions',
  description: null,
  appliedValue: 1000,
  defaultValue: 1000,
  unit: 'Count',
  periodValue: null,
  periodUnit: null,
  adjustable: true,
  globalQuota: false,
  hasUsageMetric: true,
  usage: 850,
  usageSource: null,
  usageBasis: 'peak',
  usageNote: null,
  utilization: 85,
  consoleUrl: 'https://console',
  requestable: true,
};

function text(container: HTMLElement): string {
  return (container.textContent ?? '').replace(/\s+/g, ' ');
}

function button(container: HTMLElement, label: RegExp): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find(b => label.test(b.textContent ?? ''));
  if (!found) throw new Error(`no button ${label}`);
  return found as HTMLButtonElement;
}

function type(container: HTMLElement, value: string): void {
  const el = container.querySelector('#aws-limit-desired') as HTMLInputElement;
  el.value = value;
  el.dispatchEvent(new Event('input'));
  TestBed.tick();
}

async function settle(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0));
  TestBed.tick();
}

describe('RequestIncreaseDialogComponent', () => {
  let http: { requestIncrease: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();
    http = {
      requestIncrease: vi.fn().mockResolvedValue({
        requestId: 'req-1', serviceCode: 'lambda', quotaCode: 'L-B99A9384', status: 'CASE_OPENED',
        caseId: 'case-42', requestedBy: 'admin@example.edu', consoleUrl: 'c',
      }),
    };
    TestBed.configureTestingModule({ providers: [{ provide: AwsLimitsHttpService, useValue: http }] });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(limit: AwsLimit = LIMIT) {
    const opened = await openInCdkDialog<RequestIncreaseDialogComponent, RequestIncreaseDialogData, RequestIncreaseDialogResult>(
      RequestIncreaseDialogComponent,
      { data: { limit, region: 'us-west-2', guidanceUrl: 'https://docs/aws-limits/' } },
    );
    TestBed.tick();
    return opened;
  }

  it('is a named dialog showing the quota facts read-only', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Request AWS increase' });
    expect(text(container)).toContain('AWS Lambda (lambda)');
    expect(text(container)).toContain('us-west-2');
    expect(container.querySelector('[data-testid="applied-value"]')?.textContent).toContain('1,000');
    expect(text(container)).toContain('several business days');
  });

  it('refuses a value that is not above the applied value', async () => {
    const { container } = await open();
    type(container, '1000');
    button(container, /Review request/).click();
    TestBed.tick();
    expect(text(container)).toContain('Must be greater than the applied value (1,000)');
    expect(container.querySelector('[data-testid="confirmation"]')).toBeNull();
    expect(http.requestIncrease).not.toHaveBeenCalled();
  });

  it('confirms, submits, shows the status and case, and returns the record', async () => {
    const { container, ref } = await open();
    type(container, '3000');
    button(container, /Review request/).click();
    TestBed.tick();
    expect(container.querySelector('[data-testid="confirmation"]')?.textContent).toContain('3,000');
    expect(http.requestIncrease).not.toHaveBeenCalled();

    button(container, /Submit to AWS/).click();
    await settle();
    expect(http.requestIncrease).toHaveBeenCalledWith({ serviceCode: 'lambda', quotaCode: 'L-B99A9384', desiredValue: 3000 });
    const result = container.querySelector('[data-testid="result"]')?.textContent ?? '';
    expect(result).toContain('Case opened');
    expect(result).toContain('case-42');

    const closed = firstValueFrom(ref.closed);
    button(container, /Done/).click();
    expect((await closed)?.requestId).toBe('req-1');
  });

  it('shows the server message when AWS refuses, and stays open', async () => {
    http.requestIncrease.mockRejectedValue(
      new HttpErrorResponse({ status: 409, error: { detail: 'An increase request for this quota is already open.' } }),
    );
    const { container } = await open();
    type(container, '3000');
    button(container, /Review request/).click();
    TestBed.tick();
    button(container, /Submit to AWS/).click();
    await settle();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('already open');
    expect(button(container, /Submit to AWS/)).toBeTruthy();
  });

  it('closes with nothing when cancelled', async () => {
    const { container, ref } = await open();
    const closed = firstValueFrom(ref.closed);
    button(container, /Cancel/).click();
    expect(await closed).toBeUndefined();
  });
});
