"""Assertions for PepOracleCicdStack — GitHub OIDC provider + a deploy role whose
trust is restricted to this repo's v* tag refs and its `production` environment,
and whose only power is to assume the CDK bootstrap roles."""
from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from pep_oracle_infra.cicd_stack import PepOracleCicdStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")


def _template() -> Template:
    app = cdk.App()
    return Template.from_stack(PepOracleCicdStack(app, "Cicd", env=ENV))


def test_oidc_provider_present():
    _template().resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 1)


def test_deploy_role_trust_scoped_to_repo_tag_refs_and_environment():
    t = _template()
    t.has_resource_properties("AWS::IAM::Role", Match.object_like({
        "AssumeRolePolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                        },
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": [
                                "repo:greggkp/pep-oracle:ref:refs/tags/v*",
                                "repo:greggkp/pep-oracle:environment:production",
                            ]
                        },
                    },
                }),
            ])
        })
    }))


def test_deploy_role_can_only_assume_cdk_bootstrap_roles():
    j = json.dumps(_template().to_json())
    assert "sts:AssumeRole" in j
    assert "cdk-hnb659fds-*" in j
    assert '"Action":"*"' not in j.replace(" ", "")  # never blanket admin
