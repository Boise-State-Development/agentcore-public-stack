import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { ToolFormPage } from './tool-form.page';
import { AdminToolService } from '../services/admin-tool.service';
import { ConnectorsService } from '../../connectors/services/connectors.service';

/**
 * Phase 5 (#419): the protocol='mcp' Gateway target section of the admin tool
 * form — that onSubmit builds the correct mcpGatewayConfig payload per
 * credential type, and that a 502 (Gateway target failed) is surfaced
 * distinctly from a 400 (validation).
 */
describe('ToolFormPage — Gateway target (protocol=mcp)', () => {
  let adminToolService: { createTool: ReturnType<typeof vi.fn>; updateTool: ReturnType<typeof vi.fn>; fetchTool: ReturnType<typeof vi.fn> };

  function makeComponent(): ToolFormPage {
    adminToolService = {
      createTool: vi.fn().mockResolvedValue({}),
      updateTool: vi.fn().mockResolvedValue({}),
      fetchTool: vi.fn(),
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

  function fillBaseGatewayForm(cmp: ToolFormPage): void {
    cmp.form.patchValue({
      toolId: 'gw_weather',
      displayName: 'Weather (Gateway)',
      description: 'Weather via the AgentCore Gateway',
      protocol: 'mcp',
      gwTargetName: 'weather-target',
      gwEndpointUrl: 'https://example.com/mcp',
    });
  }

  it('builds an IAM gateway config and submits it', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.addGwTool();
    cmp.gwToolsArray.at(0).patchValue({ name: 'get_forecast', needsApproval: true });

    await cmp.onSubmit();

    expect(adminToolService.createTool).toHaveBeenCalledTimes(1);
    const payload = adminToolService.createTool.mock.calls[0][0];
    expect(payload.protocol).toBe('mcp');
    expect(payload.mcpGatewayConfig).toEqual({
      targetName: 'weather-target',
      endpointUrl: 'https://example.com/mcp',
      listingMode: 'default',
      credentialType: 'gateway_iam_role',
      credentialProviderArn: null,
      oauthScopes: [],
      grantType: 'authorization_code',
      customParameters: null,
      tools: [{ name: 'get_forecast', needsApproval: true, description: null }],
    });
  });

  it('builds an OAuth gateway config (ARN + parsed scopes) and forces DEFAULT listing', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    // Set DYNAMIC first, then switch to OAuth — co-gating must force DEFAULT.
    cmp.form.patchValue({ gwListingMode: 'dynamic' });
    cmp.form.patchValue({
      gwCredentialType: 'oauth',
      gwCredentialProviderArn: 'arn:aws:bedrock-agentcore:us-west-2:1:token-vault/default/oauth2credentialprovider/gh',
      gwOauthScopes: 'repo read:user',
      gwGrantType: 'client_credentials',
    });

    await cmp.onSubmit();

    const cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.credentialType).toBe('oauth');
    expect(cfg.listingMode).toBe('default');
    expect(cfg.credentialProviderArn).toContain('oauth2credentialprovider/gh');
    expect(cfg.oauthScopes).toEqual(['repo', 'read:user']);
    expect(cfg.grantType).toBe('client_credentials');
  });

  it('surfaces a 502 (Gateway target failed) distinctly from a 400 (validation)', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);

    adminToolService.createTool.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 502, error: { detail: 'CreateGatewayTarget failed' } }),
    );
    await cmp.onSubmit();
    expect(cmp.error()).toContain('Gateway target operation failed');
    expect(cmp.error()).toContain('CreateGatewayTarget failed');

    adminToolService.createTool.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 400, error: { detail: 'mcp_gateway_config required' } }),
    );
    await cmp.onSubmit();
    expect(cmp.error()).toContain('Validation error');
  });
});
