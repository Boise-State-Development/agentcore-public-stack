# IAM Patterns

## Service Role

```typescript
import * as iam from 'aws-cdk-lib/aws-iam';
import { getResourceName } from './config';

const serviceRole = new iam.Role(this, 'AppApiRole', {
  roleName: getResourceName(config, 'app-api-role'),
  assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
  description: 'Execution role for App API ECS tasks',
});
```

## Policy Statements with SIDs

Always use descriptive SIDs for clarity and auditability:

```typescript
serviceRole.addToPolicy(new iam.PolicyStatement({
  sid: 'DynamoDBReadWrite',
  effect: iam.Effect.ALLOW,
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

serviceRole.addToPolicy(new iam.PolicyStatement({
  sid: 'S3ReadWrite',
  effect: iam.Effect.ALLOW,
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

## ECS Task Execution Role

Separate from task role - handles pulling images and logging:

```typescript
const executionRole = new iam.Role(this, 'ExecutionRole', {
  roleName: getResourceName(config, 'app-api-execution-role'),
  assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
  description: 'Execution role for ECS task definition',
  managedPolicies: [
    iam.ManagedPolicy.fromAwsManagedPolicyName(
      'service-role/AmazonECSTaskExecutionRolePolicy'
    ),
  ],
});

// If using Secrets Manager for container secrets
executionRole.addToPolicy(new iam.PolicyStatement({
  sid: 'SecretsManagerAccess',
  actions: ['secretsmanager:GetSecretValue'],
  resources: [`${secret.secretArn}*`],  // Wildcard for random suffix
}));
```

## Lambda Execution Role

```typescript
const lambdaRole = new iam.Role(this, 'LambdaRole', {
  roleName: getResourceName(config, 'my-lambda-role'),
  assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  description: 'Execution role for My Lambda function',
});

// Basic Lambda execution (CloudWatch Logs)
lambdaRole.addManagedPolicy(
  iam.ManagedPolicy.fromAwsManagedPolicyName(
    'service-role/AWSLambdaBasicExecutionRole'
  )
);

// VPC access if Lambda is in VPC
lambdaRole.addManagedPolicy(
  iam.ManagedPolicy.fromAwsManagedPolicyName(
    'service-role/AWSLambdaVPCAccessExecutionRole'
  )
);
```

## Bedrock AgentCore Roles

### Runtime Role
```typescript
const runtimeRole = new iam.Role(this, 'RuntimeRole', {
  roleName: getResourceName(config, 'agentcore-runtime-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AWS Bedrock AgentCore Runtime',
});

// Model invocation
runtimeRole.addToPolicy(new iam.PolicyStatement({
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

// Code Interpreter access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'CodeInterpreterAccess',
  actions: [
    'bedrock-agentcore:InvokeCodeInterpreter',
    'bedrock-agentcore:CreateCodeInterpreterSession',
  ],
  resources: [codeInterpreterArn],
}));

// Browser access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'BrowserAccess',
  actions: ['bedrock-agentcore:InvokeBrowser'],
  resources: [browserArn],
}));

// Gateway access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'GatewayAccess',
  actions: ['bedrock-agentcore:InvokeGateway'],
  resources: [gatewayArn],
}));
```

### Memory Role
```typescript
const memoryRole = new iam.Role(this, 'MemoryRole', {
  roleName: getResourceName(config, 'agentcore-memory-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AgentCore Memory',
});

memoryRole.addToPolicy(new iam.PolicyStatement({
  sid: 'BedrockModelAccess',
  actions: [
    'bedrock:InvokeModel',
    'bedrock:InvokeModelWithResponseStream',
  ],
  resources: ['arn:aws:bedrock:*::foundation-model/*'],
}));
```

### Gateway Role
```typescript
const gatewayRole = new iam.Role(this, 'GatewayRole', {
  roleName: getResourceName(config, 'agentcore-gateway-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AgentCore MCP Gateway',
});

// Allow invoking Lambda functions
gatewayRole.addToPolicy(new iam.PolicyStatement({
  sid: 'LambdaInvoke',
  actions: ['lambda:InvokeFunction'],
  resources: [
    `arn:aws:lambda:${config.awsRegion}:${config.awsAccount}:function:${config.projectPrefix}-mcp-*`,
  ],
}));
```

## Granting Permissions (Preferred)

Use CDK grant methods when available:

```typescript
// DynamoDB
table.grantReadWriteData(role);
table.grantReadData(role);
table.grantWriteData(role);

// S3
bucket.grantReadWrite(role);
bucket.grantRead(role);
bucket.grantPut(role);
bucket.grantDelete(role);

// Secrets Manager
secret.grantRead(role);

// KMS
key.grantEncryptDecrypt(role);

// SQS
queue.grantSendMessages(role);
queue.grantConsumeMessages(role);

// SNS
topic.grantPublish(role);
```

## Cross-Account Access

```typescript
const crossAccountRole = new iam.Role(this, 'CrossAccountRole', {
  roleName: getResourceName(config, 'cross-account-role'),
  assumedBy: new iam.AccountPrincipal('123456789012'),
  description: 'Role for cross-account access',
  externalIds: ['unique-external-id'],  // For security
});
```

## Service-Linked Roles

```typescript
// Some services require service-linked roles
// They're created automatically, but you can reference them
const serviceLinkedRole = iam.Role.fromRoleName(
  this,
  'EcsServiceLinkedRole',
  'AWSServiceRoleForECS'
);
```

## Condition Keys

```typescript
role.addToPolicy(new iam.PolicyStatement({
  sid: 'RestrictedS3Access',
  actions: ['s3:GetObject'],
  resources: [`${bucket.bucketArn}/*`],
  conditions: {
    StringEquals: {
      's3:ExistingObjectTag/classification': 'public',
    },
    IpAddress: {
      'aws:SourceIp': ['10.0.0.0/8'],
    },
  },
}));
```

## Permission Boundaries

```typescript
const permissionBoundary = new iam.ManagedPolicy(this, 'PermissionBoundary', {
  managedPolicyName: getResourceName(config, 'permission-boundary'),
  statements: [
    new iam.PolicyStatement({
      sid: 'AllowedServices',
      actions: [
        'dynamodb:*',
        's3:*',
        'logs:*',
      ],
      resources: ['*'],
    }),
  ],
});

const role = new iam.Role(this, 'BoundedRole', {
  assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  permissionsBoundary: permissionBoundary,
});
```

## Important: Secrets Manager ARN Wildcard

AWS appends a random 6-character suffix to secret ARNs:

```typescript
// CORRECT - includes wildcard
role.addToPolicy(new iam.PolicyStatement({
  actions: ['secretsmanager:GetSecretValue'],
  resources: [`${secret.secretArn}*`],
}));

// INCORRECT - will fail to match
role.addToPolicy(new iam.PolicyStatement({
  actions: ['secretsmanager:GetSecretValue'],
  resources: [secret.secretArn],
}));
```
