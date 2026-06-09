"""CI/CD bootstrap stack (Phase 4): a GitHub OIDC provider + a least-privilege
deploy role assumable ONLY from this repo's v* tag refs or its `production`
GitHub Environment. Deployed once, manually, with admin creds (the pipeline
can't create its own trust). The GitHub Actions deploy workflow then assumes the
role keylessly — no long-lived AWS secrets.

The role's only power is to assume the CDK bootstrap roles (cdk-hnb659fds-*);
CDK performs all resource mutations through those, so this role needs nothing
broader. us-east-1 is reachable too, for PepOracleProdStack's cross-region cert
reference."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_REPO = "greggkp/pep-oracle"
DEPLOY_ENVIRONMENT = "production"
# The deploy role is assumable from exactly two scopes (never refs/heads/*):
#   1. v* TAG ref pushes — the original tag-push release path.
#   2. the `production` GitHub Environment — so a workflow_dispatch release cut
#      from main (which the session CAN trigger) can assume the role without
#      trusting arbitrary main-branch runs.
# The deploy job declares `environment: production`, so a run from EITHER trigger
# presents an allowed `sub`. Keeping the tag pattern means tag-push deploys still
# work even on GitHub plans where Environments are unavailable on private repos.
SUBJECTS = [
    f"repo:{GITHUB_REPO}:ref:refs/tags/v*",
    f"repo:{GITHUB_REPO}:environment:{DEPLOY_ENVIRONMENT}",
]


class PepOracleCicdStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        provider = iam.OpenIdConnectProvider(
            self, "GitHubOidc",
            url=GITHUB_OIDC_URL,
            client_ids=["sts.amazonaws.com"],
        )

        principal = iam.OpenIdConnectPrincipal(
            provider,
            conditions={
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                },
                "StringLike": {
                    "token.actions.githubusercontent.com:sub": SUBJECTS,
                },
            },
        )

        role = iam.Role(
            self, "DeployRole",
            role_name="pep-oracle-github-deploy",
            assumed_by=principal,
            max_session_duration=Duration.hours(1),
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*"],
        ))
        # cdk reads the bootstrap version param with the caller's own identity.
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:*:{self.account}:parameter/cdk-bootstrap/hnb659fds/version"],
        ))

        CfnOutput(self, "DeployRoleArn", value=role.role_arn)
        self.deploy_role = role
