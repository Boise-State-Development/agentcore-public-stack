import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface EcsClusterConstructProps {
  config: AppConfig;
  vpc: ec2.IVpc;
}

/**
 * EcsClusterConstruct — single ECS cluster shared by all Fargate services.
 *
 * Today only `app_api` uses this cluster. The inference-api runs on
 * AgentCore Runtime, not Fargate, and does not register with the cluster.
 *
 * SSM parameters published (cross-stack reads kept identical to legacy
 * for Phase 2 equivalence):
 *
 *   /{prefix}/network/ecs-cluster-name
 *   /{prefix}/network/ecs-cluster-arn
 */
export class EcsClusterConstruct extends Construct {
  public readonly ecsCluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: EcsClusterConstructProps) {
    super(scope, id);

    const { config, vpc } = props;

    this.ecsCluster = new ecs.Cluster(this, 'EcsCluster', {
      clusterName: getResourceName(config, 'ecs-cluster'),
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    new ssm.StringParameter(this, 'EcsClusterNameParameter', {
      parameterName: `/${config.projectPrefix}/network/ecs-cluster-name`,
      stringValue: this.ecsCluster.clusterName,
      description: 'ECS Cluster Name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
