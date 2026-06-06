import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { ToolFormPage } from './tool-form.page';
import { AdminToolService } from '../services/admin-tool.service';
import { ConnectorsService } from '../../connectors/services/connectors.service';
import {
  detectAwsServiceFromUrl,
  extractAwsRegionFromUrl,
} from '../models/admin-tool.model';

/**
 * Phase 5 (#419): the protocol='mcp' Gateway target section of the admin tool
 * form — that onSubmit builds the correct mcpGatewayConfig payload per
 * credential type, and that a 502 (Gateway target failed) is surfaced
 * distinctly from a 400 (validation).
 */
describe('ToolFormPage — Gateway target (protocol=mcp)', () => {
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

  it('builds a public (none) gateway config by default', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.addGwTool();
    cmp.gwToolsArray.at(0).patchValue({ name: 'get_forecast', needsApproval: true });

    await cmp.onSubmit();

    expect(adminToolService.createTool).toHaveBeenCalledTimes(1);
    const cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.credentialType).toBe('none');
    expect(cfg.credentialProviderArn).toBeNull();
    expect(cfg.awsService).toBeNull();
    expect(cfg.tools).toEqual([{ name: 'get_forecast', needsApproval: true, description: null }]);
  });

  it('builds an IAM gateway config with aws service', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.form.patchValue({ gwCredentialType: 'gateway_iam_role', gwAwsService: 'lambda', gwAwsRegion: 'us-west-2' });

    await cmp.onSubmit();

    const cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.credentialType).toBe('gateway_iam_role');
    expect(cfg.awsService).toBe('lambda');
    expect(cfg.awsRegion).toBe('us-west-2');
    expect(cfg.credentialProviderArn).toBeNull();
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

  it('discovers tools from the gateway endpoint and merges them into the rows', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.form.patchValue({ gwCredentialType: 'gateway_iam_role', gwAwsService: 'lambda' });
    // Pre-existing manual row whose approval flag must be preserved on merge.
    cmp.addGwTool();
    cmp.gwToolsArray.at(0).patchValue({ name: 'get_forecast', needsApproval: true });

    adminToolService.discoverMCPTools.mockResolvedValueOnce({
      tools: [
        { name: 'get_forecast', description: 'forecast' },
        { name: 'set_alert', description: 'writes' },
      ],
    });

    await cmp.discoverGatewayTools();

    // IAM target → discovery signs with aws-iam against the endpoint URL.
    expect(adminToolService.discoverMCPTools).toHaveBeenCalledWith(
      expect.objectContaining({ serverUrl: 'https://example.com/mcp', authType: 'aws-iam' }),
    );
    const names = cmp.gwToolsArray.controls.map((c) => c.get('name')?.value);
    expect(names).toEqual(['get_forecast', 'set_alert']);
    // Existing approval flag preserved; not duplicated.
    expect(cmp.gwToolsArray.at(0).get('needsApproval')?.value).toBe(true);
  });

  it('maps non-IAM credential types to an unauthenticated discovery attempt', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.form.patchValue({
      gwCredentialType: 'oauth',
      gwCredentialProviderArn: 'arn:...:oauth2credentialprovider/x',
    });

    await cmp.discoverGatewayTools();

    expect(adminToolService.discoverMCPTools).toHaveBeenCalledWith(
      expect.objectContaining({ authType: 'none' }),
    );
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

  it('auto-derives aws service + region from a Lambda URL when IAM is selected', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    // Pick IAM, then enter an AWS-hosted endpoint — fields should self-populate.
    cmp.form.patchValue({ gwCredentialType: 'gateway_iam_role' });
    cmp.form.patchValue({
      gwEndpointUrl: 'https://abc123.lambda-url.us-east-1.on.aws/mcp',
    });

    expect(cmp.form.get('gwAwsService')?.value).toBe('lambda');
    expect(cmp.form.get('gwAwsRegion')?.value).toBe('us-east-1');

    await cmp.onSubmit();
    const cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.awsService).toBe('lambda');
    expect(cfg.awsRegion).toBe('us-east-1');
  });

  it('does not clobber a hand-edited aws service when the URL changes', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.form.patchValue({ gwCredentialType: 'gateway_iam_role' });
    cmp.form.patchValue({ gwEndpointUrl: 'https://abc.lambda-url.us-west-2.on.aws/mcp' });
    expect(cmp.form.get('gwAwsService')?.value).toBe('lambda');

    // Admin overrides the service, then edits the URL — override must survive.
    cmp.form.get('gwAwsService')?.setValue('my-private-service');
    cmp.form.patchValue({ gwEndpointUrl: 'https://def.execute-api.eu-west-1.amazonaws.com/mcp' });

    expect(cmp.form.get('gwAwsService')?.value).toBe('my-private-service');
    // Region wasn't overridden, so it still tracks the URL.
    expect(cmp.form.get('gwAwsRegion')?.value).toBe('eu-west-1');
  });

  it('falls back to deriving aws service at save when the field is blank', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    // Simulate legacy/edit state: IAM with a known URL but a blank service.
    cmp.form.patchValue({ gwCredentialType: 'gateway_iam_role' });
    cmp.form.patchValue({ gwEndpointUrl: 'https://gw.bedrock-agentcore.us-west-2.amazonaws.com/mcp' });
    cmp.form.get('gwAwsService')?.setValue('', { emitEvent: false });
    cmp.form.get('gwAwsRegion')?.setValue('', { emitEvent: false });

    await cmp.onSubmit();
    const cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.awsService).toBe('bedrock-agentcore');
    expect(cfg.awsRegion).toBe('us-west-2');
  });

  it('leaves the aws service blank for an unrecognised (custom-domain) host', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);
    cmp.form.patchValue({ gwCredentialType: 'gateway_iam_role' });
    cmp.form.patchValue({ gwEndpointUrl: 'https://mcp.mycorp.example.com/mcp' });

    expect(cmp.form.get('gwAwsService')?.value).toBe('');
    expect(cmp.form.get('gwAwsRegion')?.value).toBe('');
  });

  it('sends lambdaFunctionName for an IAM Lambda-URL target, null otherwise', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);

    // IAM + Lambda URL → the function name is sent so the backend can grant invoke.
    cmp.form.patchValue({
      gwCredentialType: 'gateway_iam_role',
      gwEndpointUrl: 'https://abc.lambda-url.us-west-2.on.aws/mcp',
      gwLambdaFunctionName: 'mcp-class-search-dev',
    });
    expect(cmp.isLambdaUrlEndpoint()).toBe(true);
    await cmp.onSubmit();
    let cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.lambdaFunctionName).toBe('mcp-class-search-dev');

    // Non-Lambda endpoint → not applicable, sent as null even if a stale value lingers.
    adminToolService.createTool.mockClear();
    cmp.form.patchValue({ gwEndpointUrl: 'https://mcp.example.com/mcp' });
    expect(cmp.isLambdaUrlEndpoint()).toBe(false);
    await cmp.onSubmit();
    cfg = adminToolService.createTool.mock.calls[0][0].mcpGatewayConfig;
    expect(cfg.lambdaFunctionName).toBeNull();
  });

  it('recommends IAM only when an AWS-hosted endpoint is left on None', async () => {
    const cmp = makeComponent();
    await cmp.ngOnInit();
    fillBaseGatewayForm(cmp);

    // None + AWS host → recommend IAM.
    cmp.form.patchValue({
      gwCredentialType: 'none',
      gwEndpointUrl: 'https://abc.lambda-url.us-west-2.on.aws/mcp',
    });
    expect(cmp.showIamRecommendation()).toBe(true);

    // None + custom domain → no recommendation (we can't tell it needs IAM).
    cmp.form.patchValue({ gwEndpointUrl: 'https://mcp.example.com/mcp' });
    expect(cmp.showIamRecommendation()).toBe(false);

    // Already on IAM → nothing to recommend.
    cmp.form.patchValue({
      gwCredentialType: 'gateway_iam_role',
      gwEndpointUrl: 'https://abc.lambda-url.us-west-2.on.aws/mcp',
    });
    expect(cmp.showIamRecommendation()).toBe(false);
  });
});

describe('AWS endpoint derivation helpers', () => {
  it('detects the AWS service from known endpoint hosts', () => {
    expect(detectAwsServiceFromUrl('https://x.lambda-url.us-west-2.on.aws/mcp')).toBe('lambda');
    expect(detectAwsServiceFromUrl('https://x.execute-api.us-east-1.amazonaws.com/p')).toBe('execute-api');
    expect(detectAwsServiceFromUrl('https://g.bedrock-agentcore.eu-west-1.amazonaws.com/')).toBe(
      'bedrock-agentcore',
    );
  });

  it('returns empty string for an unrecognised host (no lambda default)', () => {
    expect(detectAwsServiceFromUrl('https://mcp.example.com/mcp')).toBe('');
    expect(detectAwsServiceFromUrl('')).toBe('');
  });

  it('extracts the region from known endpoint hosts', () => {
    expect(extractAwsRegionFromUrl('https://x.lambda-url.ap-south-1.on.aws/mcp')).toBe('ap-south-1');
    expect(extractAwsRegionFromUrl('https://x.execute-api.us-east-2.amazonaws.com/p')).toBe('us-east-2');
    expect(extractAwsRegionFromUrl('https://mcp.example.com/mcp')).toBe('');
  });
});
