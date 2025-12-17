#!/usr/bin/env node
/**
 * CDK App for Quota Management Infrastructure
 *
 * Usage:
 *   npm run cdk deploy -- QuotaStack-dev
 *   npm run cdk deploy -- QuotaStack-prod --context environment=prod
 */

import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { QuotaStack } from '../lib/stacks/quota-stack';

const app = new cdk.App();

// Get environment from context (default: dev)
const environment = app.node.tryGetContext('environment') || 'dev';

// Get AWS account and region from environment or use defaults
const account = process.env.CDK_DEFAULT_ACCOUNT;
const region = process.env.CDK_DEFAULT_REGION || 'us-east-1';

// Create QuotaStack
new QuotaStack(app, `QuotaStack-${environment}`, {
  environment,
  env: {
    account,
    region,
  },
  description: `Quota Management DynamoDB tables for ${environment} environment`,
});

app.synth();
