/**
 * Health-check cadence on the app-api target group and container.
 *
 * On 2026-10-30 production lost app-api tasks to a native crash and to a
 * synchronous scan that held the event loop. Both were found by the health
 * checks, but at 30 s x 3 the ALB kept sending requests to a dead task for
 * about 90 s. These values are the detection budget; the constants are read
 * from the construct so the test and the template cannot drift apart.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import {
  APP_API_ALB_HEALTH_INTERVAL_SECONDS,
  APP_API_ALB_HEALTHY_THRESHOLD,
  APP_API_ALB_UNHEALTHY_THRESHOLD,
  APP_API_CONTAINER_HEALTH_INTERVAL_SECONDS,
  APP_API_CONTAINER_HEALTH_RETRIES,
} from '../lib/constructs/app-api/app-api-service-construct';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

describe('app-api health checks', () => {
  let template: Template;

  beforeAll(() => {
    const config = createMockConfig();
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  it('the budget itself: a hung task is pulled in ~20 s, not ~90 s', () => {
    expect(APP_API_ALB_HEALTH_INTERVAL_SECONDS * APP_API_ALB_UNHEALTHY_THRESHOLD).toBe(20);
    expect(APP_API_CONTAINER_HEALTH_INTERVAL_SECONDS * APP_API_CONTAINER_HEALTH_RETRIES).toBe(30);
    // A replacement task takes traffic after 20 s of passing probes.
    expect(APP_API_ALB_HEALTH_INTERVAL_SECONDS * APP_API_ALB_HEALTHY_THRESHOLD).toBe(20);
  });

  it('target group probes /health every 10 s, 2 to fail, 2 to recover, 5 s timeout', () => {
    const groups = template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup', {
      Properties: { Name: `${MOCK_PREFIX}-app-api-tg` },
    });
    const [tg] = Object.values(groups);
    expect(tg).toBeDefined();
    expect(tg.Properties.HealthCheckPath).toBe('/health');
    expect(tg.Properties.HealthCheckIntervalSeconds).toBe(APP_API_ALB_HEALTH_INTERVAL_SECONDS);
    expect(tg.Properties.HealthCheckTimeoutSeconds).toBe(5);
    expect(tg.Properties.HealthyThresholdCount).toBe(APP_API_ALB_HEALTHY_THRESHOLD);
    expect(tg.Properties.UnhealthyThresholdCount).toBe(APP_API_ALB_UNHEALTHY_THRESHOLD);
    // The ALB requires timeout < interval; CloudFormation rejects the deploy otherwise.
    expect(tg.Properties.HealthCheckTimeoutSeconds).toBeLessThan(tg.Properties.HealthCheckIntervalSeconds);
  });

  it('keeps the 30 s deregistration delay for in-flight requests', () => {
    const groups = template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup', {
      Properties: { Name: `${MOCK_PREFIX}-app-api-tg` },
    });
    const [tg] = Object.values(groups);
    const attrs = tg.Properties.TargetGroupAttributes as Array<{ Key: string; Value: string }>;
    expect(attrs).toContainEqual({ Key: 'deregistration_delay.timeout_seconds', Value: '30' });
  });

  it('container health check runs every 10 s with 3 retries and an unchanged 60 s start period', () => {
    const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
    const appApi = Object.values(taskDefs)
      .flatMap((td) => td.Properties.ContainerDefinitions as Array<Record<string, any>>)
      .find((c) => c.Name === 'app-api');
    expect(appApi).toBeDefined();
    expect(appApi!.HealthCheck).toEqual({
      Command: expect.arrayContaining(['CMD-SHELL']),
      Interval: APP_API_CONTAINER_HEALTH_INTERVAL_SECONDS,
      Timeout: 5,
      Retries: APP_API_CONTAINER_HEALTH_RETRIES,
      StartPeriod: 60,
    });
  });
});
