/**
 * Typed bundle of every PlatformStack resource the compute
 * constructs (InferenceAgentCoreConstruct + AppApiServiceConstruct)
 * need at synth time. PlatformStack.wireCompute() builds this once
 * and passes it through, replacing the previous pattern of
 * `ssm.StringParameter.valueForStringParameter()` calls inside the
 * compute constructs.
 *
 * Why this exists: `valueForStringParameter` synthesises a
 * `AWS::SSM::Parameter::Value<String>` CFN parameter. CFN resolves
 * those parameters BEFORE any of the stack's resources are created.
 * On a fresh first deploy, the SSM params don't exist yet — they
 * would be created by the same stack's other constructs — so CFN
 * fails with "Unable to fetch parameters from parameter store".
 * Passing typed construct refs sidesteps the parameter-resolution
 * stage entirely; the values are just strings/refs at synth time.
 *
 * Note on the IAM grant calls below: where a granteeRole is
 * required (e.g. table.grantReadWriteData(role)), do that on the
 * underlying construct (`refs.usersTable.grantReadWriteData(...)`)
 * rather than constructing a policy by ARN. CDK auto-derives the
 * resource ARN and the index ARN, and tracks the grant in the
 * stack graph — fewer string-shaped surprises.
 */

import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

export interface PlatformComputeRefs {
  // ── Network
  vpc: ec2.IVpc;
  alb: elbv2.IApplicationLoadBalancer;
  albListener: elbv2.IApplicationListener;
  albSecurityGroup: ec2.ISecurityGroup;
  ecsCluster: ecs.ICluster;

  // ── Identity / Cognito
  userPool: cognito.IUserPool;
  bffAppClient: cognito.IUserPoolClient;
  bffAppClientSecret: secretsmanager.ISecret;
  cognitoDomain: cognito.UserPoolDomain;
  cognitoIssuerUrl: string;        // resolved at synth from userPool ref
  cognitoDomainUrl: string;        // resolved at synth from cognitoDomain
  authSecret: secretsmanager.ISecret;
  voiceTicketSigningSecret: secretsmanager.ISecret;
  voiceTicketReplayTable: dynamodb.ITable;
  bffCookieSigningKey: kms.IKey;
  bffCookieDataKeySecret: secretsmanager.ISecret;
  platformWorkloadIdentity: bedrock.CfnWorkloadIdentity;

  // ── OAuth / federated identity
  oauthProvidersTable: dynamodb.ITable;
  oauthUserTokensTable: dynamodb.ITable;
  oauthTokenEncryptionKey: kms.IKey;
  oauthClientSecretsSecret: secretsmanager.ISecret;
  authProvidersTable: dynamodb.ITable;
  authProviderSecretsSecret: secretsmanager.ISecret;

  // ── Application data tables
  oidcStateTable: dynamodb.ITable;
  bffSessionsTable: dynamodb.ITable;
  usersTable: dynamodb.ITable;
  appRolesTable: dynamodb.ITable;
  apiKeysTable: dynamodb.ITable;
  userQuotasTable: dynamodb.ITable;
  quotaEventsTable: dynamodb.ITable;
  sessionsMetadataTable: dynamodb.ITable;
  userCostSummaryTable: dynamodb.ITable;
  systemCostRollupTable: dynamodb.ITable;
  managedModelsTable: dynamodb.ITable;
  userSettingsTable: dynamodb.ITable;
  userMenuLinksTable: dynamodb.ITable;
  systemPromptsTable: dynamodb.ITable;
  sharedConversationsTable: dynamodb.ITable;
  fileUploadBucket: s3.IBucket;
  fileUploadTable: dynamodb.ITable;

  // ── RAG
  ragDocumentsBucket: s3.IBucket;
  ragAssistantsTable: dynamodb.ITable;
  ragVectorBucketName: string;
  ragVectorIndexName: string;

  // ── Artifacts
  artifactsContentBucket: s3.IBucket;
  artifactsTable: dynamodb.ITable;
  artifactRenderTokenSecret: secretsmanager.ISecret;
  artifactsOriginUrl: string;

  // ── Skills (admin-managed) — S3-backed reference files (PR-4)
  skillResourcesBucket: s3.IBucket;

  // ── Fine-tuning
  fineTuningJobsTable: dynamodb.ITable;
  fineTuningAccessTable: dynamodb.ITable;
  fineTuningDataBucket: s3.IBucket;

  // ── AgentCore (Memory, Code Interpreter, Browser)
  agentCoreMemoryArn: string;
  agentCoreMemoryId: string;
  agentCoreCodeInterpreterArn: string;
  agentCoreCodeInterpreterId: string;
  agentCoreBrowserArn: string;
  agentCoreBrowserId: string;

  // ── MCP sandbox edge
  mcpSandboxProxyOrigin: string;
}
