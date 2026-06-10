import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { CfnResource } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import { AppConfig, applyStandardTags } from './config';

// Network
import { AlbConstruct } from './constructs/network/alb-construct';
import { EcsClusterConstruct } from './constructs/network/ecs-cluster-construct';
import { NetworkConstruct } from './constructs/network/network-construct';

// Identity
import { ArtifactRenderTokenSecretConstruct } from './constructs/identity/artifact-render-token-secret-construct';
import { AuthProvidersConstruct } from './constructs/identity/auth-providers-construct';
import { AuthSecretConstruct } from './constructs/identity/auth-secret-construct';
import { BffCookieKeyConstruct } from './constructs/identity/bff-cookie-key-construct';
import { CognitoConstruct } from './constructs/identity/cognito-construct';
import { OAuthTablesConstruct } from './constructs/identity/oauth-tables-construct';
import { PlatformIdentityConstruct } from './constructs/identity/platform-identity-construct';
import { VoiceTicketConstruct } from './constructs/identity/voice-ticket-construct';

// Data
import { AdminTablesConstruct } from './constructs/data/admin-tables-construct';
import { AuthTablesConstruct } from './constructs/data/auth-tables-construct';
import { CostTrackingTablesConstruct } from './constructs/data/cost-tracking-tables-construct';
import { FileUploadConstruct } from './constructs/data/file-upload-construct';
import { QuotaTablesConstruct } from './constructs/data/quota-tables-construct';
import { SharedConversationsConstruct } from './constructs/data/shared-conversations-construct';

// RAG (data half lives in Platform)
import { RagDataConstruct } from './constructs/rag/rag-data-construct';
import { RagIngestionLambdaConstruct } from './constructs/rag-ingestion/rag-ingestion-lambda-construct';

// Artifacts (data + render Lambda + CloudFront distribution).
// Lambda + distribution + Route53 alias here. The Lambda's
// configuration is CDK-owned, but its handler code is shipped
// out-of-band by the backend workflow (`update-function-code`),
// so Platform deploys do not redeploy the Lambda's code.
import { ArtifactsDataConstruct } from './constructs/artifacts/artifacts-data-construct';
import { ArtifactRenderLambdaConstruct } from './constructs/artifacts/artifact-render-lambda-construct';
import { ArtifactsDistributionConstruct } from './constructs/artifacts/artifacts-distribution-construct';
import { SkillResourcesConstruct } from './constructs/skills/skill-resources-construct';

// AgentCore (Memory, Code Interpreter, Browser, Gateway).
// Pure infrastructure — no code, no out-of-band updates needed.
// The Runtime itself stays in PlatformStack; it will move
// here in a follow-up phase when the bootstrap-container pattern
// is in place.
import { AgentCoreMemoryConstruct } from './constructs/agentcore/memory-construct';
import { AgentCoreCodeInterpreterConstruct } from './constructs/agentcore/code-interpreter-construct';
import { AgentCoreBrowserConstruct } from './constructs/agentcore/browser-construct';
import { AgentCoreGatewayConstruct } from './constructs/gateway/agentcore-gateway-construct';

// MCP sandbox (S3 + CloudFront — Platform edge surface)
import { McpSandboxBucketConstruct } from './constructs/mcp-sandbox/mcp-sandbox-bucket-construct';
import { McpSandboxDistributionConstruct } from './constructs/mcp-sandbox/mcp-sandbox-distribution-construct';

// Fine-tuning (data half lives in Platform)
import { FineTuningDataConstruct } from './constructs/fine-tuning/fine-tuning-data-construct';

// SPA (frontend bucket + CloudFront — Platform edge surface)
import { SpaBucketConstruct } from './constructs/spa/spa-bucket-construct';
import { SpaDistributionConstruct } from './constructs/spa/spa-distribution-construct';
import { RagCorsUpdaterConstruct } from './constructs/spa/rag-cors-updater-construct';

// Zones
import { AlbDnsConstruct } from './constructs/zones/alb-dns-construct';

// Compute constructs (absorbed from the old BackendStack in Phase 7
// exists). After the collapse, all resources — data, edge,
// AgentCore, compute — live in this one stack.
import { AppApiServiceConstruct } from './constructs/app-api/app-api-service-construct';
import { InferenceAgentCoreConstruct } from './constructs/inference-api/inference-agentcore-construct';
import { PlatformComputeRefs } from './constructs/platform-compute-refs';
import { SageMakerExecutionRoleConstruct } from './constructs/fine-tuning/sagemaker-execution-role-construct';

export interface PlatformStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * PlatformStack — every non-compute resource the application needs.
 *
 * Owns: VPC, ALB, ECS cluster, Cognito, every shared DynamoDB table,
 * every data S3 bucket (file upload, RAG, fine-tuning, SPA static,
 * artifacts content, mcp-sandbox shell), CloudFront distributions
 * (SPA, artifacts, mcp-sandbox), Route53 hosted zone + ACM cert +
 * alias records, Secrets Manager (auth secret, BFF cookie data key,
 * voice ticket signing, optional artifact render-token, OAuth client
 * secrets, auth provider secrets, Cognito BFF client secret),
 * shared `WorkloadIdentity`, KMS keys.
 *
 * Exposes typed `public readonly` properties so the wireCompute()
 * flow can pass them as explicit construct props at instantiation
 * time. This avoids the same-stack `valueForStringParameter`
 * deadlock — CFN resolves SSM template parameters before any of
 * this stack's resources are created, so a sibling construct
 * reading a param this stack publishes is unsatisfiable on first
 * deploy.
 *
 * The render Lambda is part of the compute layer in PlatformStack
 * and consumes `artifactsContentBucket` + `artifactsTable` +
 * `artifactRenderTokenSecret` via typed prop passing. The
 * artifacts CloudFront distribution is also in PlatformStack (its
 * origin is the render Lambda Function URL, so they must share
 * a stack to avoid a circular dependency).
 */
export class PlatformStack extends cdk.Stack {
  // ── Network
  public readonly vpc: ec2.IVpc;
  public readonly alb: elbv2.IApplicationLoadBalancer;
  public readonly albListener: elbv2.IApplicationListener;
  public readonly albSecurityGroup: ec2.ISecurityGroup;
  public readonly ecsCluster: ecs.ICluster;

  // ── Identity / crypto
  public readonly authSecret: secretsmanager.ISecret;
  public readonly voiceTicketSigningSecret: secretsmanager.ISecret;
  public readonly voiceTicketReplayTable: dynamodb.ITable;
  public readonly bffCookieSigningKey: kms.IKey;
  public readonly bffCookieDataKeySecret: secretsmanager.ISecret;
  public readonly platformWorkloadIdentity: bedrock.CfnWorkloadIdentity;
  public readonly oauthProvidersTable: dynamodb.ITable;
  public readonly oauthUserTokensTable: dynamodb.ITable;
  public readonly oauthTokenEncryptionKey: kms.IKey;
  public readonly oauthClientSecretsSecret: secretsmanager.ISecret;
  public readonly authProvidersTable: dynamodb.ITable;
  public readonly authProviderSecretsSecret: secretsmanager.ISecret;
  public readonly userPool: cognito.IUserPool;
  public readonly bffAppClient: cognito.IUserPoolClient;
  public readonly bffAppClientSecret: secretsmanager.ISecret;
  public readonly cognitoDomain: cognito.UserPoolDomain;

  // ── Data tables
  public readonly oidcStateTable: dynamodb.ITable;
  public readonly bffSessionsTable: dynamodb.ITable;
  public readonly usersTable: dynamodb.ITable;
  public readonly appRolesTable: dynamodb.ITable;
  public readonly apiKeysTable: dynamodb.ITable;
  public readonly userQuotasTable: dynamodb.ITable;
  public readonly quotaEventsTable: dynamodb.ITable;
  public readonly sessionsMetadataTable: dynamodb.ITable;
  public readonly userCostSummaryTable: dynamodb.ITable;
  public readonly systemCostRollupTable: dynamodb.ITable;
  public readonly managedModelsTable: dynamodb.ITable;
  public readonly userSettingsTable: dynamodb.ITable;
  public readonly userMenuLinksTable: dynamodb.ITable;
  public readonly systemPromptsTable: dynamodb.ITable;
  public readonly sharedConversationsTable: dynamodb.ITable;
  public readonly fileUploadBucket: s3.IBucket;
  public readonly fileUploadTable: dynamodb.ITable;

  // ── RAG (data half)
  public readonly ragDocumentsBucket: s3.IBucket;
  public readonly ragAssistantsTable: dynamodb.ITable;
  public readonly ragVectorBucketName: string;
  public readonly ragVectorIndexName: string;
  public readonly ragVectorBucket: CfnResource;
  public readonly ragVectorIndex: CfnResource;

  // ── SPA edge
  public readonly spaBucket: s3.IBucket;
  public readonly spaDistribution: cloudfront.IDistribution;
  public readonly spaDistributionDomainName: string;

  // ── MCP sandbox edge (always-on)
  public readonly mcpSandboxBucket: s3.IBucket;
  public readonly mcpSandboxDistribution: cloudfront.IDistribution;
  public readonly mcpSandboxProxyOrigin: string;

  // ── Artifacts
  public readonly artifactsContentBucket: s3.IBucket;
  public readonly artifactsTable: dynamodb.ITable;
  public readonly artifactRenderTokenSecret: secretsmanager.ISecret;
  /**
   * The CSP `frame-ancestors` source list resolved for the artifacts
   * iframe origin (space-separated). Forwarded to the render Lambda
   * via its `FRAME_ANCESTOR_ORIGIN` env var so it stays byte-
   * identical with the CloudFront response-headers-policy.
   */
  public readonly artifactsFrameAncestors: string;
  /**
   * Origin URL where the artifacts iframe is served
   * (`https://artifacts.{domain}`). Exposed for PlatformStack's App
   * API to forward to the Fargate container as `ARTIFACTS_ORIGIN`
   * PlatformStack now owns the Fargate task def — once it moves to
   * Platform, this typed ref isn't needed for cross-stack.
   */
  public readonly artifactsOriginUrl: string;

  // ── Skills (admin-managed) — S3-backed reference files (PR-4)
  public readonly skillResourcesBucket: s3.IBucket;

  // ── Fine-tuning
  public readonly fineTuningJobsTable: dynamodb.ITable;
  public readonly fineTuningAccessTable: dynamodb.ITable;
  public readonly fineTuningDataBucket: s3.IBucket;

  // ── AgentCore (Memory, Code Interpreter, Browser)
  // Pure infra — no code attached to these. The Runtime that
  // *uses* them lives in PlatformStack; it consumes these
  // typed refs to avoid a same-stack SSM round-trip there.
  public readonly agentCoreMemory: bedrock.CfnMemory;
  public readonly agentCoreMemoryArn: string;
  public readonly agentCoreMemoryId: string;
  public readonly agentCoreCodeInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly agentCoreCodeInterpreterArn: string;
  public readonly agentCoreCodeInterpreterId: string;
  public readonly agentCoreBrowser: bedrock.CfnBrowserCustom;
  public readonly agentCoreBrowserArn: string;
  public readonly agentCoreBrowserId: string;

  // ── Internal handles for the two-step wiring methods
  private readonly _config: AppConfig;
  private readonly _spaBucketConstruct: SpaBucketConstruct;
  private readonly _mcpSandboxBucketConstruct: McpSandboxBucketConstruct;
  private readonly _artifactsDataConstruct: ArtifactsDataConstruct;
  private readonly _albDns!: AlbDnsConstruct;

  constructor(scope: Construct, id: string, props: PlatformStackProps) {
    super(scope, id, props);

    const { config } = props;
    this._config = config;
    applyStandardTags(this, config);

    // ============================================================
    // Network
    // ============================================================
    const network = new NetworkConstruct(this, 'Network', { config });
    this.vpc = network.vpc;

    const alb = new AlbConstruct(this, 'Alb', { config, vpc: this.vpc });
    this.alb = alb.alb;
    this.albListener = alb.albListener;
    this.albSecurityGroup = alb.albSecurityGroup;

    const ecsCluster = new EcsClusterConstruct(this, 'EcsCluster', {
      config,
      vpc: this.vpc,
    });
    this.ecsCluster = ecsCluster.ecsCluster;

    // ============================================================
    // Identity / crypto
    // ============================================================
    const authSecret = new AuthSecretConstruct(this, 'AuthSecret', { config });
    this.authSecret = authSecret.authSecret;

    const voice = new VoiceTicketConstruct(this, 'VoiceTicket', { config });
    this.voiceTicketSigningSecret = voice.signingSecret;
    this.voiceTicketReplayTable = voice.replayTable;

    const bffCookie = new BffCookieKeyConstruct(this, 'BffCookieKey', {
      config,
    });
    this.bffCookieSigningKey = bffCookie.signingKey;
    this.bffCookieDataKeySecret = bffCookie.dataKeySecret;

    const platformIdentity = new PlatformIdentityConstruct(
      this,
      'PlatformIdentity',
      { config },
    );
    this.platformWorkloadIdentity = platformIdentity.workloadIdentity;

    const oauth = new OAuthTablesConstruct(this, 'OAuthTables', { config });
    this.oauthProvidersTable = oauth.providersTable;
    this.oauthUserTokensTable = oauth.userTokensTable;
    this.oauthTokenEncryptionKey = oauth.tokenEncryptionKey;
    this.oauthClientSecretsSecret = oauth.clientSecretsSecret;

    const authProviders = new AuthProvidersConstruct(this, 'AuthProviders', {
      config,
    });
    this.authProvidersTable = authProviders.providersTable;
    this.authProviderSecretsSecret = authProviders.secretsSecret;

    const cognitoConstruct = new CognitoConstruct(this, 'Cognito', { config });
    this.userPool = cognitoConstruct.userPool;
    this.bffAppClient = cognitoConstruct.bffAppClient;
    this.bffAppClientSecret = cognitoConstruct.bffAppClientSecret;
    this.cognitoDomain = cognitoConstruct.cognitoDomain;

    const artifactRenderToken = new ArtifactRenderTokenSecretConstruct(
      this,
      'ArtifactRenderToken',
      { config },
    );
    this.artifactRenderTokenSecret = artifactRenderToken.secret;

    // ============================================================
    // Data tables
    // ============================================================
    const authTables = new AuthTablesConstruct(this, 'AuthTables', { config });
    this.oidcStateTable = authTables.oidcStateTable;
    this.bffSessionsTable = authTables.bffSessionsTable;
    this.usersTable = authTables.usersTable;
    this.appRolesTable = authTables.appRolesTable;
    this.apiKeysTable = authTables.apiKeysTable;

    const quotaTables = new QuotaTablesConstruct(this, 'QuotaTables', {
      config,
    });
    this.userQuotasTable = quotaTables.userQuotasTable;
    this.quotaEventsTable = quotaTables.quotaEventsTable;

    const costTrackingTables = new CostTrackingTablesConstruct(
      this,
      'CostTrackingTables',
      { config },
    );
    this.sessionsMetadataTable = costTrackingTables.sessionsMetadataTable;
    this.userCostSummaryTable = costTrackingTables.userCostSummaryTable;
    this.systemCostRollupTable = costTrackingTables.systemCostRollupTable;
    this.managedModelsTable = costTrackingTables.managedModelsTable;

    const adminTables = new AdminTablesConstruct(this, 'AdminTables', {
      config,
    });
    this.userSettingsTable = adminTables.userSettingsTable;
    this.userMenuLinksTable = adminTables.userMenuLinksTable;
    this.systemPromptsTable = adminTables.systemPromptsTable;

    const fileUpload = new FileUploadConstruct(this, 'FileUpload', { config });
    this.fileUploadBucket = fileUpload.bucket;
    this.fileUploadTable = fileUpload.table;

    const sharedConversations = new SharedConversationsConstruct(
      this,
      'SharedConversations',
      { config },
    );
    this.sharedConversationsTable = sharedConversations.table;

    // ============================================================
    // RAG data
    // ============================================================
    const ragData = new RagDataConstruct(this, 'RagData', { config });
    this.ragDocumentsBucket = ragData.documentsBucket;
    this.ragAssistantsTable = ragData.assistantsTable;
    this.ragVectorBucketName = ragData.vectorBucketName;
    this.ragVectorIndexName = ragData.vectorIndexName;
    this.ragVectorBucket = ragData.vectorBucket;
    this.ragVectorIndex = ragData.vectorIndex;

    // ============================================================
    // RAG ingestion Lambda
    //
    // the old BackendStack to PlatformStack. Same model as
    // artifact-render in Phase 3: CDK ships the Lambda's
    // *configuration* with a stable bootstrap container image; the
    // workflow ships the *real* image out-of-band via
    // `aws lambda update-function-code --image-uri`.
    //
    // The S3 ObjectCreated subscription on the documents bucket is
    // wired here too, since both bucket and Lambda live in this
    // stack now (no more cross-stack notification dance).
    // ============================================================
    const ragIngestion = new RagIngestionLambdaConstruct(
      this,
      'RagIngestion',
      {
        config,
        documentsBucket: this.ragDocumentsBucket,
        assistantsTable: this.ragAssistantsTable,
        vectorBucketName: this.ragVectorBucketName,
        vectorIndexName: this.ragVectorIndexName,
      },
    );

    this.ragDocumentsBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(ragIngestion.lambda),
      { prefix: 'assistants/' },
    );

    // ============================================================
    // Fine-tuning data
    // ============================================================
    const fineTuningData = new FineTuningDataConstruct(
      this,
      'FineTuningData',
      { config },
    );
    this.fineTuningJobsTable = fineTuningData.jobsTable;
    this.fineTuningAccessTable = fineTuningData.accessTable;
    this.fineTuningDataBucket = fineTuningData.dataBucket;

    // ============================================================
    // Artifacts data (distribution wired in later via
    // `wireArtifactsDistribution`)
    // ============================================================
    this._artifactsDataConstruct = new ArtifactsDataConstruct(
      this,
      'ArtifactsData',
      { config },
    );
    this.artifactsContentBucket = this._artifactsDataConstruct.bucket;
    this.artifactsTable = this._artifactsDataConstruct.table;

    // ============================================================
    // Skill reference-file storage (admin-managed Skills, PR-4).
    // Bytes-only S3 bucket; the skill catalog row (app-roles table)
    // carries the lightweight manifest. Threaded to the compute roles
    // via PlatformComputeRefs.skillResourcesBucket below.
    // ============================================================
    this.skillResourcesBucket = new SkillResourcesConstruct(
      this,
      'SkillResources',
      { config },
    ).bucket;

    const artifactsDomainName = config.domainName!;
    this.artifactsFrameAncestors = [
      `https://${artifactsDomainName}`,
      ...config.artifacts.extraFrameAncestors,
    ].join(' ');

    // ============================================================
    // Artifact render Lambda + CloudFront distribution + Route53
    //
    // Lambda owns its CDK configuration (runtime, IAM, env vars,
    // function URL, log group) but NOT its code — that's shipped
    // out-of-band by the backend workflow's
    // `scripts/build/deploy-artifact-render-code.sh` step using
    // `aws lambda update-function-code`. Same model as the SPA
    // (Platform owns bucket + CloudFront; the workflow does
    // `aws s3 sync` + invalidation).
    //
    // CDK uses a stable bootstrap asset (a 503 placeholder); CFN
    // sees no change to the Lambda's `Code` property on subsequent
    // Platform deploys and leaves the out-of-band-deployed real
    // handler untouched.
    // ============================================================
    const artifactRenderLambda = new ArtifactRenderLambdaConstruct(
      this,
      'ArtifactRender',
      {
        config,
        artifactsTable: this.artifactsTable,
        artifactsBucket: this.artifactsContentBucket,
        renderTokenSecret: this.artifactRenderTokenSecret,
        frameAncestors: this.artifactsFrameAncestors,
      },
    );

    const artifactsDistribution = new ArtifactsDistributionConstruct(
      this,
      'ArtifactsDistribution',
      {
        config,
        renderFunctionUrl: artifactRenderLambda.functionUrl,
        frameAncestors: this.artifactsFrameAncestors,
      },
    );
    this.artifactsOriginUrl = artifactsDistribution.originUrl;

    // ============================================================
    // AgentCore Memory + Code Interpreter + Browser
    //
    // Pure-infrastructure AgentCore resources. They have no "code"
    // to redeploy, take 5-15 minutes to create (Memory in particular),
    // and rarely change. They live here so:
    //   1. Backend can deploy without recreating them on every push.
    //   2. Memory's transitional-state errors only affect the once-
    //      ever first Platform deploy, not subsequent code deploys.
    //   3. The Runtime in PlatformStack consumes them via typed cross-
    //      stack refs (no same-stack SSM round-trip).
    // ============================================================
    const agentCoreMemoryConstruct = new AgentCoreMemoryConstruct(
      this,
      'AgentCoreMemory',
      { config },
    );
    this.agentCoreMemory = agentCoreMemoryConstruct.memory;
    this.agentCoreMemoryArn = agentCoreMemoryConstruct.memoryArn;
    this.agentCoreMemoryId = agentCoreMemoryConstruct.memoryId;

    const agentCoreCodeInterpreterConstruct = new AgentCoreCodeInterpreterConstruct(
      this,
      'AgentCoreCodeInterpreter',
      { config },
    );
    this.agentCoreCodeInterpreter = agentCoreCodeInterpreterConstruct.codeInterpreter;
    this.agentCoreCodeInterpreterArn = agentCoreCodeInterpreterConstruct.codeInterpreterArn;
    this.agentCoreCodeInterpreterId = agentCoreCodeInterpreterConstruct.codeInterpreterId;

    const agentCoreBrowserConstruct = new AgentCoreBrowserConstruct(
      this,
      'AgentCoreBrowser',
      { config },
    );
    this.agentCoreBrowser = agentCoreBrowserConstruct.browser;
    this.agentCoreBrowserArn = agentCoreBrowserConstruct.browserArn;
    this.agentCoreBrowserId = agentCoreBrowserConstruct.browserId;

    // AgentCore Gateway — config-only (MCP protocol, AWS_IAM
    // authorizer, IAM execution role with invoke rights against the
    // /^${prefix}-mcp-/ Lambda naming convention used by the
    // external mcp-servers repo). No code lives here — Gateway
    // Targets are managed out-of-band by mcp-servers' own deploy.
    new AgentCoreGatewayConstruct(this, 'AgentCoreGateway', { config });

    // ============================================================
    // MCP sandbox edge (always-on; bucket+dist; everything is wired
    // up here because nothing else needs to be threaded back from
    // another stack — the shell is static)
    // ============================================================
    this._mcpSandboxBucketConstruct = new McpSandboxBucketConstruct(
      this,
      'McpSandboxBucket',
      { config },
    );
    this.mcpSandboxBucket = this._mcpSandboxBucketConstruct.bucket;

    const mcpSandboxDist = new McpSandboxDistributionConstruct(
      this,
      'McpSandboxDistribution',
      { config, bucket: this.mcpSandboxBucket },
    );
    this.mcpSandboxDistribution = mcpSandboxDist.distribution;
    this.mcpSandboxProxyOrigin = mcpSandboxDist.proxyOrigin;

    this._mcpSandboxBucketConstruct.deployShell(mcpSandboxDist.distribution);

    // ============================================================
    // SPA bucket + CloudFront distribution.
    // ============================================================
    this._spaBucketConstruct = new SpaBucketConstruct(this, 'SpaBucket', {
      config,
    });
    this.spaBucket = this._spaBucketConstruct.bucket;

    // ============================================================
    // ALB DNS / hosted zone (Route53 lookup + ALB URL export).
    // ============================================================
    this._albDns = new AlbDnsConstruct(this, 'AlbDns', {
      config,
      alb: this.alb,
    });

    // ============================================================
    // SPA CloudFront distribution. The `/api/*` behavior origins to
    // the ALB; AlbDns provides the resolved URL via a same-stack
    // ref, so no SSM round-trip needed.
    // ============================================================
    const spaDist = new SpaDistributionConstruct(this, 'SpaDistribution', {
      config,
      bucket: this.spaBucket,
      appApiUrl: this._albDns.albUrl,
    });
    this.spaDistribution = spaDist.distribution;
    this.spaDistributionDomainName = spaDist.distributionDomainName;

    // ============================================================
    // RAG CORS updater — patches the RAG documents bucket CORS to
    // accept the resolved frontend URL.
    // ============================================================
    const frontendUrl = this._config.domainName
      ? `https://${this._config.domainName}`
      : `https://${this.spaDistributionDomainName}`;
    new RagCorsUpdaterConstruct(this, 'RagCorsUpdater', {
      config: this._config,
      frontendUrl,
      documentsBucket: this.ragDocumentsBucket,
    });
  }

  /**
   * Wire the application compute layer (Inference AgentCore Runtime,
   * SageMaker fine-tuning IAM, App API Fargate service).
   *
   * Construction order below is for readability only — CDK token
   * resolution and the CFN dependency graph handle the actual
   * dependency ordering at synth/deploy time. Don't try to
   * "parallelize" by reordering blindly; the listed order matches
   * how a reader naturally follows data flow:
   *   1. InferenceApi — exposes runtimeEndpointUrl
   *   2. SageMaker    — exposes executionRole + security group +
   *                     private subnet IDs
   *   3. AppApi       — consumes refs from both of the above.
   */
  public wireCompute(): void {
    // Build the typed refs bundle once. Used by both compute
    // constructs to source their dependencies from typed CDK refs
    // instead of `valueForStringParameter` (which deadlocks on
    // first deploy because CFN parameter resolution runs before
    // resource creation in a single-stack architecture).
    const refs: PlatformComputeRefs = {
      vpc: this.vpc,
      alb: this.alb,
      albListener: this.albListener,
      albSecurityGroup: this.albSecurityGroup,
      ecsCluster: this.ecsCluster,
      userPool: this.userPool,
      bffAppClient: this.bffAppClient,
      bffAppClientSecret: this.bffAppClientSecret,
      cognitoDomain: this.cognitoDomain,
      cognitoIssuerUrl: `https://cognito-idp.${this._config.awsRegion}.amazonaws.com/${this.userPool.userPoolId}`,
      cognitoDomainUrl: `https://${this.cognitoDomain.domainName}.auth.${this._config.awsRegion}.amazoncognito.com`,
      authSecret: this.authSecret,
      voiceTicketSigningSecret: this.voiceTicketSigningSecret,
      voiceTicketReplayTable: this.voiceTicketReplayTable,
      bffCookieSigningKey: this.bffCookieSigningKey,
      bffCookieDataKeySecret: this.bffCookieDataKeySecret,
      platformWorkloadIdentity: this.platformWorkloadIdentity,
      oauthProvidersTable: this.oauthProvidersTable,
      oauthUserTokensTable: this.oauthUserTokensTable,
      oauthTokenEncryptionKey: this.oauthTokenEncryptionKey,
      oauthClientSecretsSecret: this.oauthClientSecretsSecret,
      authProvidersTable: this.authProvidersTable,
      authProviderSecretsSecret: this.authProviderSecretsSecret,
      oidcStateTable: this.oidcStateTable,
      bffSessionsTable: this.bffSessionsTable,
      usersTable: this.usersTable,
      appRolesTable: this.appRolesTable,
      apiKeysTable: this.apiKeysTable,
      userQuotasTable: this.userQuotasTable,
      quotaEventsTable: this.quotaEventsTable,
      sessionsMetadataTable: this.sessionsMetadataTable,
      userCostSummaryTable: this.userCostSummaryTable,
      systemCostRollupTable: this.systemCostRollupTable,
      managedModelsTable: this.managedModelsTable,
      userSettingsTable: this.userSettingsTable,
      userMenuLinksTable: this.userMenuLinksTable,
      systemPromptsTable: this.systemPromptsTable,
      sharedConversationsTable: this.sharedConversationsTable,
      fileUploadBucket: this.fileUploadBucket,
      fileUploadTable: this.fileUploadTable,
      ragDocumentsBucket: this.ragDocumentsBucket,
      ragAssistantsTable: this.ragAssistantsTable,
      ragVectorBucketName: this.ragVectorBucketName,
      ragVectorIndexName: this.ragVectorIndexName,
      artifactsContentBucket: this.artifactsContentBucket,
      artifactsTable: this.artifactsTable,
      artifactRenderTokenSecret: this.artifactRenderTokenSecret,
      artifactsOriginUrl: this.artifactsOriginUrl,
      skillResourcesBucket: this.skillResourcesBucket,
      fineTuningJobsTable: this.fineTuningJobsTable,
      fineTuningAccessTable: this.fineTuningAccessTable,
      fineTuningDataBucket: this.fineTuningDataBucket,
      agentCoreMemoryArn: this.agentCoreMemoryArn,
      agentCoreMemoryId: this.agentCoreMemoryId,
      agentCoreCodeInterpreterArn: this.agentCoreCodeInterpreterArn,
      agentCoreCodeInterpreterId: this.agentCoreCodeInterpreterId,
      agentCoreBrowserArn: this.agentCoreBrowserArn,
      agentCoreBrowserId: this.agentCoreBrowserId,
      mcpSandboxProxyOrigin: this.mcpSandboxProxyOrigin,
    };

    const inferenceApi = new InferenceAgentCoreConstruct(this, 'InferenceApi', {
      config: this._config,
      refs,
      memoryArn: this.agentCoreMemoryArn,
      memoryId: this.agentCoreMemoryId,
      codeInterpreterArn: this.agentCoreCodeInterpreterArn,
      codeInterpreterId: this.agentCoreCodeInterpreterId,
      browserArn: this.agentCoreBrowserArn,
      browserId: this.agentCoreBrowserId,
    });

    const sagemakerPrivateSubnetIds = this.vpc.privateSubnets
      .map((s) => s.subnetId)
      .join(',');
    const sagemaker = new SageMakerExecutionRoleConstruct(this, 'FineTuning', {
      config: this._config,
      dataBucket: this.fineTuningDataBucket,
      jobsTable: this.fineTuningJobsTable,
      vpc: this.vpc,
      privateSubnetIdsString: sagemakerPrivateSubnetIds,
    });

    new AppApiServiceConstruct(this, 'AppApi', {
      config: this._config,
      refs,
      agentCoreMemoryArn: this.agentCoreMemoryArn,
      agentCoreMemoryId: this.agentCoreMemoryId,
      inferenceApiRuntimeEndpointUrl: inferenceApi.runtimeEndpointUrl,
      artifactsOrigin: this.artifactsOriginUrl,
      sagemakerExecutionRoleArn: sagemaker.executionRole.roleArn,
      sagemakerSecurityGroupId: sagemaker.securityGroup.securityGroupId,
      sagemakerPrivateSubnetIds,
    });
  }
}
