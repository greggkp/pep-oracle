"""us-east-1 stack: Route 53 hosted zone for the MCP domain + the CloudFront ACM cert.

CloudFront requires its ACM cert in us-east-1, so the zone+cert live here and the
prod stack (ap-southeast-2) references the cert ARN cross-region. Resources are
added in Task 7.
"""

from __future__ import annotations

from aws_cdk import Stack
from constructs import Construct

from pep_oracle_infra.config import DeployConfig


class PepOracleCertStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: DeployConfig, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg
        # Task 7 adds: PublicHostedZone + Certificate (DNS-validated).
