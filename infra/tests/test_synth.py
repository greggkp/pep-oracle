"""Synth smoke test: both stacks synthesize to a CloudFormation template."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Template

from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.prod_stack import PepOracleProdStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")
CERT_ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="pep-oracle-test",
        allowed_email="me@example.com",
    )


def test_prod_stack_synthesizes():
    app = cdk.App()
    stack = PepOracleProdStack(
        app, "Prod", cfg=_cfg(),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        hosted_zone_id="Z123456ABCDEFG",
        hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True, env=ENV,
    )
    Template.from_stack(stack)  # raises if synthesis fails


def test_cert_stack_synthesizes():
    app = cdk.App()
    stack = PepOracleCertStack(app, "Cert", cfg=_cfg(), cross_region_references=True, env=CERT_ENV)
    Template.from_stack(stack)
