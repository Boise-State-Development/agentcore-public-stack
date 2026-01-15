# Lambda Function Patterns

## Basic Lambda Function

```typescript
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import { getResourceName } from './config';

const lambdaFunction = new lambda.Function(this, 'MyFunction', {
  functionName: getResourceName(config, 'my-function'),
  description: 'Description of what this function does',

  runtime: lambda.Runtime.PYTHON_3_13,
  handler: 'lambda_function.lambda_handler',
  code: lambda.Code.fromAsset('../backend/lambda-functions/my-function'),

  role: lambdaRole,
  architecture: lambda.Architecture.ARM_64,  // Cost optimization (~20% cheaper)

  timeout: cdk.Duration.seconds(60),
  memorySize: 512,

  environment: {
    LOG_LEVEL: config.gateway?.logLevel || 'INFO',
    PROJECT_PREFIX: config.projectPrefix,
  },
});
```

## Lambda Role with Least Privilege

```typescript
const lambdaRole = new iam.Role(this, 'LambdaRole', {
  roleName: getResourceName(config, 'my-function-role'),
  assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  description: 'Execution role for My Function Lambda',
});

// Basic execution role (CloudWatch Logs)
lambdaRole.addManagedPolicy(
  iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')
);
```

## Secrets Manager Access

```typescript
// IMPORTANT: AWS appends random 6-char suffix to secret ARNs
// Must use wildcard to match
lambdaRole.addToPolicy(new iam.PolicyStatement({
  sid: 'SecretsManagerAccess',
  actions: ['secretsmanager:GetSecretValue'],
  resources: [`${secret.secretArn}*`],  // Wildcard required!
}));
```

## DynamoDB Access

```typescript
lambdaRole.addToPolicy(new iam.PolicyStatement({
  sid: 'DynamoDBReadWrite',
  actions: [
    'dynamodb:GetItem',
    'dynamodb:PutItem',
    'dynamodb:UpdateItem',
    'dynamodb:DeleteItem',
    'dynamodb:Query',
    'dynamodb:Scan',
  ],
  resources: [
    table.tableArn,
    `${table.tableArn}/index/*`,
  ],
}));
```

## S3 Access

```typescript
lambdaRole.addToPolicy(new iam.PolicyStatement({
  sid: 'S3ReadWrite',
  actions: [
    's3:GetObject',
    's3:PutObject',
    's3:DeleteObject',
    's3:ListBucket',
  ],
  resources: [
    bucket.bucketArn,
    `${bucket.bucketArn}/*`,
  ],
}));
```

## Bedrock Access

```typescript
lambdaRole.addToPolicy(new iam.PolicyStatement({
  sid: 'BedrockModelInvocation',
  actions: [
    'bedrock:InvokeModel',
    'bedrock:InvokeModelWithResponseStream',
  ],
  resources: [
    'arn:aws:bedrock:*::foundation-model/*',
    `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:*`,
  ],
}));
```

## VPC Lambda (for private resources)

```typescript
const vpcLambda = new lambda.Function(this, 'VpcFunction', {
  functionName: getResourceName(config, 'vpc-function'),
  runtime: lambda.Runtime.PYTHON_3_13,
  handler: 'lambda_function.lambda_handler',
  code: lambda.Code.fromAsset('../backend/lambda-functions/vpc-function'),

  vpc: vpc,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  securityGroups: [lambdaSecurityGroup],

  timeout: cdk.Duration.seconds(60),
  memorySize: 512,
});

// Security group for Lambda
const lambdaSecurityGroup = new ec2.SecurityGroup(this, 'LambdaSg', {
  vpc: vpc,
  securityGroupName: getResourceName(config, 'lambda-sg'),
  description: 'Security group for Lambda functions',
  allowAllOutbound: true,
});
```

## Lambda for MCP Gateway

```typescript
const mcpFunction = new lambda.Function(this, 'GoogleSearchFunction', {
  functionName: getResourceName(config, 'mcp-google-search'),
  description: 'Google Custom Search for web and images',

  runtime: lambda.Runtime.PYTHON_3_13,
  handler: 'lambda_function.lambda_handler',
  code: lambda.Code.fromAsset('../backend/lambda-functions/google-search'),

  role: lambdaRole,
  architecture: lambda.Architecture.ARM_64,

  timeout: cdk.Duration.seconds(60),
  memorySize: 512,

  environment: {
    GOOGLE_CREDENTIALS_SECRET_NAME: googleSecret.secretName,
    LOG_LEVEL: config.gateway?.logLevel || 'INFO',
  },
});

// Allow Gateway to invoke Lambda
mcpFunction.addPermission('GatewayPermission', {
  principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  action: 'lambda:InvokeFunction',
  sourceArn: gatewayArn,
});
```

## Heavy Workload Lambda (ML, long-running)

```typescript
const heavyLambda = new lambda.Function(this, 'HeavyFunction', {
  functionName: getResourceName(config, 'heavy-function'),

  runtime: lambda.Runtime.PYTHON_3_13,
  handler: 'lambda_function.lambda_handler',
  code: lambda.Code.fromAsset('../backend/lambda-functions/heavy-function'),

  // Maximum resources for heavy workloads
  timeout: cdk.Duration.minutes(15),  // Maximum 15 minutes
  memorySize: 10240,  // 10 GB (maximum)
  ephemeralStorageSize: cdk.Size.gibibytes(10),  // 10 GB /tmp

  architecture: lambda.Architecture.ARM_64,
});
```

## Lambda with Environment Variables from SSM

```typescript
const lambdaFunction = new lambda.Function(this, 'MyFunction', {
  // ... other config
  environment: {
    // Reference SSM parameters
    DYNAMODB_TABLE_NAME: ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/tables/my-table-name`
    ),
    S3_BUCKET_NAME: ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/buckets/my-bucket-name`
    ),
  },
});
```

## Lambda Layers

```typescript
const layer = new lambda.LayerVersion(this, 'SharedLayer', {
  layerVersionName: getResourceName(config, 'shared-layer'),
  code: lambda.Code.fromAsset('../backend/layers/shared'),
  compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
  compatibleArchitectures: [lambda.Architecture.ARM_64],
  description: 'Shared dependencies for Lambda functions',
});

const lambdaFunction = new lambda.Function(this, 'MyFunction', {
  // ... other config
  layers: [layer],
});
```

## Event Source Mappings

```typescript
// DynamoDB Streams
lambdaFunction.addEventSourceMapping('DynamoDBTrigger', {
  eventSourceArn: table.tableStreamArn,
  startingPosition: lambda.StartingPosition.LATEST,
  batchSize: 100,
  retryAttempts: 3,
});

// SQS Queue
lambdaFunction.addEventSourceMapping('SqsTrigger', {
  eventSourceArn: queue.queueArn,
  batchSize: 10,
  reportBatchItemFailures: true,
});
```

## CloudWatch Alarms

```typescript
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';

// Error rate alarm
new cloudwatch.Alarm(this, 'LambdaErrorAlarm', {
  alarmName: getResourceName(config, 'lambda-errors'),
  metric: lambdaFunction.metricErrors({
    period: cdk.Duration.minutes(5),
  }),
  threshold: 5,
  evaluationPeriods: 2,
  treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
});

// Duration alarm
new cloudwatch.Alarm(this, 'LambdaDurationAlarm', {
  alarmName: getResourceName(config, 'lambda-duration'),
  metric: lambdaFunction.metricDuration({
    period: cdk.Duration.minutes(5),
    statistic: 'p95',
  }),
  threshold: 30000,  // 30 seconds
  evaluationPeriods: 2,
});
```
