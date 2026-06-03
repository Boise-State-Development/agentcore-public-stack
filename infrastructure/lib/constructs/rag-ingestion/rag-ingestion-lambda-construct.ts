import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';

export interface RagIngestionLambdaConstructProps {
  config: AppConfig;
  /** RAG documents bucket — granted read access + S3 event subscription. */
  documentsBucket: s3.IBucket;
  /** RAG assistants table — granted read/write data access. */
  assistantsTable: dynamodb.ITable;
  /** Vector bucket name — used for IAM grants and Lambda env. */
  vectorBucketName: string;
  /** Vector index name — used for IAM grants and Lambda env. */
  vectorIndexName: string;
}

/**
 * RagIngestionLambdaConstruct — DockerImage Lambda that processes
 * documents from S3, extracts text, chunks, generates embeddings via
 * Bedrock, and stores them in the S3 Vectors index.
 *
 * from a sibling construct to PlatformStack. The Lambda's *configuration*
 * (runtime, IAM, env vars, timeout, memory, log group) is owned by
 * CDK; its *container image* is shipped independently by the backend
 * workflow's `scripts/build/deploy-image-lambda-one.sh` step, which
 * calls `aws lambda update-function-code --image-uri ...` on the
 * function with a freshly-built image from the project's ECR repo.
 *
 * `lambda.DockerImageCode.fromImageAsset` here points at the
 * bootstrap dir (`infrastructure/bootstrap-assets/rag-ingestion/`),
 * NOT at the real `backend/Dockerfile.rag-ingestion`. The bootstrap
 * Dockerfile + handler are byte-stable, so CDK's content-hash is
 * stable, so CFN sees no change to the `Code.ImageUri` on subsequent
 * Platform deploys and leaves the out-of-band-deployed real image
 * untouched.
 *
 * Two distinct ECR repos are involved at runtime:
 *   1. `cdk-assets` (CDK-managed) — holds the bootstrap image. CDK's
 *      `fromImageAsset` automatically grants the Lambda execution
 *      role pull rights on this repo.
 *   2. `{prefix}-rag-ingestion` (project) — holds the real image.
 *      We grant pull rights explicitly below so `update-function-code
 *      --image-uri` can swap the Lambda over to it.
 *
 * IAM:
 *   - Read on the documents bucket
 *   - Read/write on the assistants table
 *   - Full s3vectors:* on the supplied vector bucket + index ARNs
 *   - bedrock:InvokeModel on
 *     `arn:aws:bedrock:{region}::foundation-model/{embeddingModel}*`
 *   - ECR pull on the project's rag-ingestion repo (for real image)
 *
 * S3 event subscription on `assistants/` prefix in the documents
 * bucket triggers the Lambda on object create.
 *
 * SSM publications:
 *   /{prefix}/rag/ingestion-lambda-arn       — consumed by services
 *     that need to invoke the Lambda
 *   /{prefix}/rag/ingestion-function-name    — consumed by the
 *     backend workflow's code-deploy step to resolve the
 *     CDK-auto-generated function name
 */
export class RagIngestionLambdaConstruct extends Construct {
  public readonly lambda: lambda.DockerImageFunction;

  constructor(
    scope: Construct,
    id: string,
    props: RagIngestionLambdaConstructProps,
  ) {
    super(scope, id);

    const {
      config,
      documentsBucket,
      assistantsTable,
      vectorBucketName,
      vectorIndexName,
    } = props;

    const ingestionLogGroup = new logs.LogGroup(this, 'RagIngestionLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.lambda = new lambda.DockerImageFunction(this, 'RagIngestionLambda', {
      // Intentionally no `functionName` — let CDK auto-generate it
      // so a failed-deploy orphan can't collide with a redeploy.
      // The deploy script resolves the function via SSM at
      // `/{prefix}/rag/ingestion-function-name` (published below).
      //
      // Bootstrap image: stable, returns 503. The real image is
      // deployed by the backend workflow.
      code: lambda.DockerImageCode.fromImageAsset(
        path.resolve(
          __dirname,
          '..',
          '..',
          '..',
          'bootstrap-assets',
          'rag-ingestion',
        ),
      ),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.seconds(config.ragIngestion.lambdaTimeout),
      memorySize: config.ragIngestion.lambdaMemorySize,
      logGroup: ingestionLogGroup,
      environment: {
        S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: documentsBucket.bucketName,
        DYNAMODB_ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
        S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME: vectorBucketName,
        S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME: vectorIndexName,
      },
      description:
        'RAG document ingestion pipeline - processes documents from S3, extracts text, chunks, generates embeddings, stores in S3 vector store',
    });

    // IAM grants
    documentsBucket.grantRead(this.lambda);
    assistantsTable.grantReadWriteData(this.lambda);

    // ECR pull on the project's rag-ingestion repo so
    // `update-function-code --image-uri` works against the real
    // image. `fromImageAsset` auto-grants pull on the CDK assets
    // repo (which holds the bootstrap), so we don't need that one.
    this.lambda.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'EcrPullProjectImage',
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        // ECR's GetAuthorizationToken doesn't accept resource scoping,
        // hence the wildcard. The other three are scoped to the
        // project's rag-ingestion repo.
        resources: ['*'],
      }),
    );

    this.lambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3vectors:ListVectorBuckets',
          's3vectors:GetVectorBucket',
          's3vectors:GetIndex',
          's3vectors:PutVectors',
          's3vectors:ListVectors',
          's3vectors:ListIndexes',
          's3vectors:GetVector',
          's3vectors:GetVectors',
          's3vectors:DeleteVector',
        ],
        resources: [
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}`,
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}/index/${vectorIndexName}`,
        ],
      }),
    );

    this.lambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${config.awsRegion}::foundation-model/${config.ragIngestion.embeddingModel}*`,
        ],
      }),
    );

    // S3 event notification is wired by the parent stack
    // (cross-stack notifications would create a circular CDK
    // dependency). When the bucket and Lambda live in the same
    // stack, the parent calls
    // `bucket.addEventNotification(s3.EventType.OBJECT_CREATED,
    // new s3n.LambdaDestination(ragIngestion.lambda), { prefix:
    // 'assistants/' })` itself.


    new ssm.StringParameter(this, 'IngestionFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/ingestion-function-name`,
      stringValue: this.lambda.functionName,
      description: 'RAG ingestion Lambda function name (CDK-auto-generated; consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
