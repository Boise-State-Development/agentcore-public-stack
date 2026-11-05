import { describe, it, expect } from 'vitest';
import {
  CONNECTOR_PRESETS,
  TENANT_CONNECTOR_TYPES,
  TENANT_ISSUER_PLACEHOLDERS,
  getConnectorPreset,
  requiresDiscovery,
  requiresTenantEndpoints,
} from './connector.model';

describe('connector presets', () => {
  it('offers each preset once', () => {
    const types = CONNECTOR_PRESETS.map(p => p.type);
    expect(new Set(types).size).toBe(types.length);
  });

  it('offers every AgentCore built-in vendor plus Custom', () => {
    const types = CONNECTOR_PRESETS.map(p => p.type).sort();
    expect(types).toEqual(
      [
        'atlassian', 'auth0', 'cognito', 'custom', 'cyberark', 'dropbox', 'facebook',
        'fusionauth', 'github', 'google', 'hubspot', 'linkedin', 'microsoft', 'notion',
        'okta', 'onelogin', 'ping', 'reddit', 'salesforce', 'slack', 'spotify',
        'twitch', 'x', 'yandex', 'zoom',
      ].sort(),
    );
  });

  it('gives each tenant vendor a preset and an issuer placeholder', () => {
    for (const type of TENANT_CONNECTOR_TYPES) {
      expect(getConnectorPreset(type)).toBeDefined();
      expect(TENANT_ISSUER_PLACEHOLDERS[type]).toMatch(/^https:\/\//);
    }
  });

  it('asks for tenant endpoints, not a discovery URL, on tenant vendors', () => {
    expect(requiresTenantEndpoints('okta')).toBe(true);
    expect(requiresDiscovery('okta')).toBe(false);
    expect(requiresTenantEndpoints('atlassian')).toBe(false);
    expect(requiresDiscovery('custom')).toBe(true);
  });
});
