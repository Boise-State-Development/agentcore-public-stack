#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { FrontendStack } from '../lib/frontend-stack';
import { AppApiStack } from '../lib/app-api-stack';
import { loadConfig, getStackEnv } from '../lib/config';

const app = new cdk.App();

// Load configuration from cdk.context.json
const config = loadConfig(app);
const env = getStackEnv(config);

// Frontend Stack - S3 + CloudFront + Route53
if (config.frontend.enabled) {
  new FrontendStack(app, 'FrontendStack', {
    config,
    env,
    description: `${config.projectPrefix} Frontend Stack - S3, CloudFront, and Route53`,
    stackName: `${config.projectPrefix}-FrontendStack`,
  });
}

// App API Stack - VPC + ALB + Fargate + Database
if (config.appApi.enabled) {
  new AppApiStack(app, 'AppApiStack', {
    config,
    env,
    description: `${config.projectPrefix} App API Stack - VPC, ALB, Fargate, and Database`,
    stackName: `${config.projectPrefix}-AppApiStack`,
  });
}

app.synth();
