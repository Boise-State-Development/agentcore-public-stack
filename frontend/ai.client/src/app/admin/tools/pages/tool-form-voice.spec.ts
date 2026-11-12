import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { ToolFormPage } from './tool-form.page';
import { AdminToolService } from '../services/admin-tool.service';
import { ConnectorsService } from '../../connectors/services/connectors.service';

/** #625: a tool runs in voice mode only when an admin marks it, and new tools start off. */
describe('ToolFormPage — available in voice mode', () => {
  let adminToolService: {
    createTool: ReturnType<typeof vi.fn>;
    updateTool: ReturnType<typeof vi.fn>;
    fetchTool: ReturnType<typeof vi.fn>;
    discoverMCPTools: ReturnType<typeof vi.fn>;
  };

  function makeComponent(): ToolFormPage {
    adminToolService = {
      createTool: vi.fn().mockResolvedValue({}),
      updateTool: vi.fn().mockResolvedValue({}),
      fetchTool: vi.fn(),
      discoverMCPTools: vi.fn().mockResolvedValue({ tools: [] }),
    };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ToolFormPage],
      providers: [
        provideRouter([]),
        { provide: AdminToolService, useValue: adminToolService },
        { provide: ConnectorsService, useValue: { getEnabledConnectors: () => [] } },
      ],
    });
    const cmp = TestBed.createComponent(ToolFormPage).componentInstance;
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    return cmp;
  }

  afterEach(() => TestBed.resetTestingModule());

  function fill(cmp: ToolFormPage, voiceAvailable?: boolean): void {
    cmp.form.patchValue({
      toolId: 'lookup_records',
      displayName: 'Lookup records',
      description: 'Looks things up',
      protocol: 'local',
      ...(voiceAvailable === undefined ? {} : { voiceAvailable }),
    });
  }

  it('creates a tool with voice off by default', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fill(cmp);
    await cmp.onSubmit();
    expect(adminToolService.createTool).toHaveBeenCalledWith(
      expect.objectContaining({ voiceAvailable: false }),
    );
  });

  it('sends voiceAvailable when the admin ticks it', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fill(cmp, true);
    await cmp.onSubmit();
    expect(adminToolService.createTool).toHaveBeenCalledWith(
      expect.objectContaining({ voiceAvailable: true }),
    );
  });
});
