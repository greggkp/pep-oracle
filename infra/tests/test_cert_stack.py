"""Template assertions for PepOracleCertStack (us-east-1)."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.config import DeployConfig

CERT_ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="b",
        cognito_domain_prefix="p",
        allowed_email="me@example.com",
    )


def _t() -> Template:
    app = cdk.App()
    s = PepOracleCertStack(app, "Cert", cfg=_cfg(), cross_region_references=True, env=CERT_ENV)
    return Template.from_stack(s)


def test_hosted_zone_for_domain():
    _t().has_resource_properties(
        "AWS::Route53::HostedZone",
        Match.object_like(
            {
                "Name": "pep-oracle.iicapn.com.",
            }
        ),
    )


def test_certificate_for_domain():
    _t().has_resource_properties(
        "AWS::CertificateManager::Certificate",
        Match.object_like(
            {
                "DomainName": "pep-oracle.iicapn.com",
            }
        ),
    )
