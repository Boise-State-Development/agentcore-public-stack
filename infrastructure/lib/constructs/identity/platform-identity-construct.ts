import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface PlatformIdentityConstructProps {
  config: AppConfig;
}

/**
 * PlatformIdentityConstruct — shared AgentCore WorkloadIdentity for
 * OAuth vault calls.
 *
 * AgentCore Runtimes auto-create their own workload identity, but it is
 * service-linked: only the runtime container itself can mint tokens
 * against it via `GetWorkloadAccessTokenForUserId`. App-api lives
 * outside the runtime gateway and was failing 500 with `WorkloadIdentity
 * is linked to a service and cannot retrieve an access token by the
 * caller`.
 *
 * We create our own workload identity here so both APIs can mint against
 * it. The OAuth token vault is keyed by (workload identity, user,
 * provider), so sharing one identity is what lets the settings-page
 * consent flow and the runtime agent loop see the same vaulted tokens —
 * a user consents once and both code paths find the token.
 *
 * The runtime's auto-created identity is left in place (we cannot tell
 * `CreateAgentRuntime` to use a pre-existing one) but is no longer used
 * for vault calls — see `_resolve_workload_token` in the backend.
 */
export class PlatformIdentityConstruct extends Construct {
  public readonly workloadIdentity: bedrock.CfnWorkloadIdentity;

  constructor(
    scope: Construct,
    id: string,
    props: PlatformIdentityConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.workloadIdentity = new bedrock.CfnWorkloadIdentity(
      this,
      'PlatformWorkloadIdentity',
      {
        name: getResourceName(config, 'platform-workload'),
      },
    );


  }
}
