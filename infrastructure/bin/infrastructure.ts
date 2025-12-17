#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { InfrastructureStack } from '../lib/infrastructure-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { AppApiStack } from '../lib/app-api-stack';
import { InferenceApiStack } from '../lib/inference-api-stack';
import { GatewayStack } from '../lib/gateway-stack';
import { loadConfig, getStackEnv } from '../lib/config';

const app = new cdk.App();

// Load configuration from cdk.context.json
const config = loadConfig(app);
const env = getStackEnv(config);

// Infrastructure Stack - VPC + ALB + ECS Cluster (DEPLOY FIRST)
new InfrastructureStack(app, 'InfrastructureStack', {
  config,
  env,
  description: `${config.projectPrefix} Infrastructure Stack - Shared Network Resources`,
  stackName: `${config.projectPrefix}-InfrastructureStack`,
});

// Frontend Stack - S3 + CloudFront + Route53
if (config.frontend.enabled) {
  new FrontendStack(app, 'FrontendStack', {
    config,
    env,
    description: `${config.projectPrefix} Frontend Stack - S3, CloudFront, and Route53`,
    stackName: `${config.projectPrefix}-FrontendStack`,
  });
}

// App API Stack - Fargate + Database
if (config.appApi.enabled) {
  new AppApiStack(app, 'AppApiStack', {
    config,
    env,
    description: `${config.projectPrefix} App API Stack - Fargate and Database`,
    stackName: `${config.projectPrefix}-AppApiStack`,
  });
}

// Inference API Stack - Fargate for AI Workloads
if (config.inferenceApi.enabled) {
  new InferenceApiStack(app, 'InferenceApiStack', {
    config,
    env,
    description: `${config.projectPrefix} Inference API Stack - Fargate for AI Workloads`,
    stackName: `${config.projectPrefix}-InferenceApiStack`,
  });
}

// Gateway Stack - Bedrock AgentCore Gateway with MCP Tools
if (config.gateway.enabled) {
  new GatewayStack(app, 'GatewayStack', {
    config,
    env,
    description: `${config.projectPrefix} Gateway Stack - Bedrock AgentCore Gateway with MCP Tools`,
    stackName: `${config.projectPrefix}-GatewayStack`,
  });
}

app.synth();
