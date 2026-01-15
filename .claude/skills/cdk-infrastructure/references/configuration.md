# Configuration Patterns

## Two-Level Configuration System

1. **Environment variables** (highest priority) - prefix: `CDK_`, `ENV_`
2. **cdk.context.json values** (fallback)
3. **Hardcoded defaults** in config.ts

## Using Configuration

```typescript
import { loadConfig, getResourceName, getStackEnv, applyStandardTags } from './config';

export class MyStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    const config = loadConfig(scope);
    super(scope, id, {
      ...props,
      env: getStackEnv(config),
      stackName: getResourceName(config, 'my-stack'),
    });

    applyStandardTags(this, config);

    // Use config values
    const cpu = config.myService.cpu;
    const memory = config.myService.memory;
  }
}
```

## Adding New Configuration

Update `lib/config.ts`:

```typescript
// 1. Add to Config interface
myService: {
  enabled: boolean;
  cpu: number;
  memory: number;
  desiredCount: number;
  maxCapacity: number;
};

// 2. Add to loadConfig function
myService: {
  enabled: parseBooleanEnv('CDK_MY_SERVICE_ENABLED') ??
    (scope.node.tryGetContext('myService')?.enabled ?? true),
  cpu: parseIntEnv('CDK_MY_SERVICE_CPU') ??
    (scope.node.tryGetContext('myService')?.cpu ?? 512),
  memory: parseIntEnv('CDK_MY_SERVICE_MEMORY') ??
    (scope.node.tryGetContext('myService')?.memory ?? 1024),
  desiredCount: parseIntEnv('CDK_MY_SERVICE_DESIRED_COUNT') ??
    (scope.node.tryGetContext('myService')?.desiredCount ?? 1),
  maxCapacity: parseIntEnv('CDK_MY_SERVICE_MAX_CAPACITY') ??
    (scope.node.tryGetContext('myService')?.maxCapacity ?? 4),
},
```

## Adding to cdk.context.json

```json
{
  "projectPrefix": "bsu-agentcore",
  "environment": "dev",
  "awsRegion": "us-west-2",
  "myService": {
    "enabled": true,
    "cpu": 512,
    "memory": 1024,
    "desiredCount": 1,
    "maxCapacity": 4
  }
}
```

## Environment Variable Overrides

```bash
# Override via environment
CDK_MY_SERVICE_ENABLED=true
CDK_MY_SERVICE_CPU=1024
ENV_MY_SERVICE_API_KEY=secret-value
```

## Naming Convention

Pattern: `{projectPrefix}-{environment-if-not-prod}-{resource-type}`

```typescript
// Production
getResourceName(config, 'vpc')  // "bsu-agentcore-vpc"

// Development
getResourceName(config, 'vpc')  // "bsu-agentcore-dev-vpc"

// With account ID for global uniqueness (S3)
getResourceName(config, 'frontend', config.awsAccount)
// "bsu-agentcore-frontend-123456789012"
```

## SSM Parameter Naming

Hierarchical naming for cross-stack references:

```
/{projectPrefix}/{category}/{resource-type}/{property}

Categories:
- /network/          - VPC, subnets, ALB, ECS cluster
- /quota/            - Quota management resources
- /cost-tracking/    - Cost tracking resources
- /auth/             - Authentication resources
- /admin/            - Admin resources
- /file-upload/      - File upload resources
- /frontend/         - Frontend resources
- /gateway/          - Gateway resources
- /app-api/          - App API resources
- /inference-api/    - Inference API resources
```

## CloudFormation Export Naming

```typescript
new cdk.CfnOutput(this, 'VpcId', {
  value: vpc.vpcId,
  description: 'VPC ID',
  exportName: `${config.projectPrefix}-VpcId`,
});
```

## Registering New Stacks

Add to `bin/infrastructure.ts`:

```typescript
import { MyNewStack } from '../lib/my-new-stack';

if (config.myNewStack?.enabled !== false) {
  new MyNewStack(app, 'MyNewStack', {
    env: getStackEnv(config),
  });
}
```
