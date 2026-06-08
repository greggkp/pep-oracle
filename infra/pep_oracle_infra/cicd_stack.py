"""CI/CD bootstrap stack (Phase 4): a GitHub OIDC provider + a least-privilege
deploy role assumable ONLY by this repo's v* tag refs. Deployed once, manually,
with admin creds (the pipeline can't create its own trust). The GitHub Actions
deploy workflow then assumes the role keylessly — no long-lived AWS secrets.

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
# Only v* TAG refs may assume the deploy role (tag push or workflow_dispatch on a tag).
SUBJECT = f"repo:{GITHUB_REPO}:ref:refs/tags/v*"


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
                    "token.actions.githubusercontent.com:sub": SUBJECT,
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
