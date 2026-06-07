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

PepOracleProdStack(
    app,
    "PepOracleProdStack",
    cfg=cfg,
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.compute_region),
)

app.synth()
