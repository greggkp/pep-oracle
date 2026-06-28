#!/usr/bin/env python3
"""CDK app entry for the pep-oracle prod serving stack (Phase 2c).

Two stacks: the us-east-1 cert/zone stack and the ap-southeast-2 prod stack, wired
with cross_region_references so the prod CloudFront can use the us-east-1 cert.
"""

import os

import aws_cdk as cdk
from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.prod_stack import PepOracleProdStack

app = cdk.App()
cfg = DeployConfig.from_node(app.node)

account = os.environ.get("CDK_DEFAULT_ACCOUNT")

cert_stack = PepOracleCertStack(
    app,
    "PepOracleCertStack",
    cfg=cfg,
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.cert_region),
)

prod = PepOracleProdStack(
    app,
    "PepOracleProdStack",
    cfg=cfg,
    cert_arn=cert_stack.certificate.certificate_arn,
    hosted_zone_id=cert_stack.hosted_zone.hosted_zone_id,
    hosted_zone_name=cert_stack.hosted_zone.zone_name,
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.compute_region),
)
prod.add_dependency(cert_stack)

from pep_oracle_infra.ingest_stack import PepOracleIngestStack  # noqa: E402

# Decoupled from PepOracleProdStack: the ingest stack imports the corpus bucket + data
# key as external resources (see ingest_stack.py), so deploying it touches only the new
# ingest resources and never the live serving Lambda. No cross-stack ref → no dependency.
PepOracleIngestStack(
    app,
    "PepOracleIngestStack",
    cfg=cfg,
    env=cdk.Environment(account=account, region=cfg.compute_region),
)

from pep_oracle_infra.cicd_stack import PepOracleCicdStack  # noqa: E402

# One-time bootstrap (deploy manually with admin creds): GitHub OIDC + deploy role.
PepOracleCicdStack(
    app,
    "PepOracleCicdStack",
    env=cdk.Environment(account=account, region=cfg.compute_region),
)

app.synth()
