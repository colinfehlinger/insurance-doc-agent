import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface ViewStackProps extends cdk.StackProps, IdaStackPropsBase {}

/**
 * LATER -- the owner's 30-second status view.
 *
 * Deliberately NOT instantiated in bin/app.ts. It exists as a named seam so the
 * shape of the system is visible from the repo layout, but it deploys nothing
 * and costs nothing until the dashboard step.
 *
 * Planned: React build on S3 behind CloudFront (OAC, no public bucket),
 * API Gateway + Lambda reading the matter table, Cognito for owner auth.
 * The view is read-mostly; the one write it needs is "acknowledge / override
 * what the agent decided", which is the human-in-the-loop hook.
 */
export class ViewStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ViewStackProps) {
    super(scope, id, props);

    // Intentionally empty. See the class doc above.
    void props.config;
  }
}
