import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';

export interface RagCorsUpdaterConstructProps {
  config: AppConfig;
  /** Frontend URL the RAG documents bucket should accept. */
  frontendUrl: string;
  /**
   * RAG documents bucket whose CORS rules should be patched.
   *
   * Passed directly (rather than looked up via SSM) because publisher
   * and consumer live in the same stack. Reading the same-stack SSM
   * parameter via `valueForStringParameter` produces an
   * `AWS::SSM::Parameter::Value<String>` template parameter that CFN
   * tries to resolve before any of the stack's resources are created,
   * which deadlocks on the first deploy.
   */
  documentsBucket: s3.IBucket;
}

/**
 * RagCorsUpdaterConstruct — custom resource that injects the deployed
 * frontend URL into the RAG documents bucket's CORS rules.
 *
 * After the SPA's CloudFront distribution lands, the RAG documents
 * bucket needs to accept PUT/GET/HEAD from the new origin so document
 * uploads work end-to-end without manual CORS configuration. The
 * Lambda fetches the existing CORS rules, augments (or appends) the
 * PUT-allowing rule with the frontend URL plus `http://localhost:4200`
 * for dev, and writes them back.
 *
 * Idempotent across re-deploys; trigger uses `Date.now()` so every
 * deployment forces a CORS refresh in case the rules drifted.
 */
export class RagCorsUpdaterConstruct extends Construct {
  constructor(
    scope: Construct,
    id: string,
    props: RagCorsUpdaterConstructProps,
  ) {
    super(scope, id);

    const { config: _config, frontendUrl, documentsBucket } = props;
    void _config;

    const ragDocumentsBucketName = documentsBucket.bucketName;
    const ragDocumentsBucketArn = documentsBucket.bucketArn;

    // Auto-generated log group name — see ArtifactRenderLambdaConstruct
    // for the same pattern + rationale.
    const updateCorsLogGroup = new logs.LogGroup(this, 'UpdateRagCorsFnLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const updateCorsHandler = new lambda.Function(this, 'UpdateRagCorsFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      logGroup: updateCorsLogGroup,
      code: lambda.Code.fromInline(`
import boto3
import cfnresponse

s3 = boto3.client('s3')

def handler(event, context):
    try:
        if event['RequestType'] in ['Create', 'Update']:
            bucket = event['ResourceProperties']['BucketName']
            frontend_url = event['ResourceProperties']['FrontendUrl']
            
            # Get existing CORS rules
            try:
                existing = s3.get_bucket_cors(Bucket=bucket)
                rules = existing.get('CORSRules', [])
            except s3.exceptions.ClientError:
                rules = []
            
            # Find or create the rule for document uploads
            found = False
            for rule in rules:
                if 'PUT' in rule.get('AllowedMethods', []):
                    # Update existing rule to include frontend URL
                    origins = set(rule.get('AllowedOrigins', []))
                    origins.add(frontend_url)
                    origins.add('http://localhost:4200')  # Keep localhost for dev
                    rule['AllowedOrigins'] = list(origins)
                    found = True
                    break
            
            if not found:
                # Create new rule
                rules.append({
                    'AllowedOrigins': [frontend_url, 'http://localhost:4200'],
                    'AllowedMethods': ['GET', 'PUT', 'HEAD'],
                    'AllowedHeaders': ['Content-Type', 'Content-Length', 'x-amz-*'],
                    'ExposeHeaders': ['ETag', 'Content-Length', 'Content-Type'],
                    'MaxAgeSeconds': 3600
                })
            
            # Update bucket CORS
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={'CORSRules': rules})
            print(f'Updated CORS for {bucket} to include {frontend_url}')
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        print(f'Error: {str(e)}')
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
`),
      timeout: cdk.Duration.minutes(2),
    });

    updateCorsHandler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetBucketCors', 's3:PutBucketCors'],
        resources: [ragDocumentsBucketArn],
      }),
    );

    new cdk.CustomResource(this, 'UpdateRagCors', {
      serviceToken: updateCorsHandler.functionArn,
      properties: {
        BucketName: ragDocumentsBucketName,
        FrontendUrl: frontendUrl,
        // Force update on every deployment by including timestamp
        Timestamp: Date.now().toString(),
      },
    });

    console.log(
      '📝 RAG documents bucket CORS will be updated with frontend URL',
    );
  }
}
