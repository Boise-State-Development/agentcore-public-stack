import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Subject, of, throwError } from 'rxjs';
import { ReleaseLedgerStateService } from './release-ledger-state.service';
import { ReleaseLedgerHttpService } from '../services/release-ledger-http.service';
import { LedgerReadout } from '../models';
import { readoutFixture, versionsFixture } from '../../../../testing/release-ledger.fixtures';

function httpStub() {
  return {
    getVersions: vi.fn(() => of(versionsFixture())),
    getSeries: vi.fn(() => of({ from: '', to: '', by: 'version', unit: 'build', series: [], bands: [], omittedKeys: 0 })),
    getQuality: vi.fn(() => of({ from: '', to: '', unit: 'build', minimumN: 20, units: [], thumbsCoverage: {}, provenanceCoverage: {} })),
    getReadout: vi.fn(() => of(readoutFixture())),
    getTools: vi.fn(() => of({ from: '', to: '', version: null, tools: [], notMeasured: ['awaitingUser'] })),
    exportNdjson: vi.fn(() => of(new Blob())),
  };
}

describe('ReleaseLedgerStateService', () => {
  let http: ReturnType<typeof httpStub>;
  let state: ReleaseLedgerStateService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    http = httpStub();
    TestBed.configureTestingModule({ providers: [{ provide: ReleaseLedgerHttpService, useValue: http }] });
    state = TestBed.inject(ReleaseLedgerStateService);
  });

  it('opens on builds when the range has one version, asking for the newest build', async () => {
    await state.load();
    expect(state.unit()).toBe('build');
    expect(http.getSeries).toHaveBeenCalledWith(expect.anything(), 'build');
    expect(http.getReadout).toHaveBeenCalledWith({ version: '1.33.0#d5ead33e05f3e229', baseline: undefined, days: 7 });
  });

  it('adopts the readout’s own candidate and baseline, and reads the tools footnote over the candidate window', async () => {
    await state.load();
    expect(state.candidateKey()).toBe('1.33.0#d5ead33e05f3e229');
    expect(state.baselineKey()).toBe('1.33.0#f200a6a3514248d3');
    await vi.waitFor(() => expect(state.tools()?.notMeasured).toEqual(['awaitingUser']));
    expect(http.getTools).toHaveBeenCalledWith({ from: '2026-11-07', to: '2026-11-07' }, '1.33.0#d5ead33e05f3e229');
  });

  it('a new candidate drops the chosen baseline so the endpoint picks the one before it', async () => {
    await state.load();
    state.selectBaseline('1.33.0#git-0123456789ab');
    state.selectCandidate('1.33.0#f200a6a3514248d3');
    expect(http.getReadout).toHaveBeenLastCalledWith({ version: '1.33.0#f200a6a3514248d3', baseline: undefined, days: 7 });
  });

  it('never lets a slower, older readout overwrite a newer one', async () => {
    await state.load();
    const slow = new Subject<LedgerReadout>();
    http.getReadout.mockReturnValueOnce(slow);
    state.selectCandidate('1.33.0#git-0123456789ab');
    const newer = readoutFixture({ candidate: { ...readoutFixture().candidate, key: '1.33.0#f200a6a3514248d3' } });
    http.getReadout.mockReturnValueOnce(of(newer));
    state.selectCandidate('1.33.0#f200a6a3514248d3');
    await vi.waitFor(() => expect(state.readout()?.candidate.key).toBe('1.33.0#f200a6a3514248d3'));
    slow.next(readoutFixture({ candidate: { ...readoutFixture().candidate, key: '1.33.0#git-0123456789ab' } }));
    slow.complete();
    await Promise.resolve();
    expect(state.readout()?.candidate.key).toBe('1.33.0#f200a6a3514248d3');
  });

  it('shows the endpoint’s own words when a unit has no rows', async () => {
    http.getReadout.mockReturnValueOnce(
      throwError(() => new HttpErrorResponse({ status: 404, error: { detail: 'no ledger rows for 1.31.0' } })),
    );
    await state.load();
    expect(state.readout()).toBeNull();
    expect(state.readoutError()).toBe('no ledger rows for 1.31.0');
    expect(state.loaded()).toBe(true);
  });

  it('changing granularity resets the comparison and reloads', async () => {
    await state.load();
    state.setUnit('version');
    expect(state.candidateKey()).toBeNull();
    // The build readout must not linger under a strip of versions.
    expect(state.readout()).toBeNull();
    await vi.waitFor(() => expect(http.getSeries).toHaveBeenLastCalledWith(expect.anything(), 'version'));
  });
});
