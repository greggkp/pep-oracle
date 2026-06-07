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

        from aws_cdk import aws_certificatemanager as acm
        from aws_cdk import aws_route53 as route53

        self.hosted_zone = route53.PublicHostedZone(
            self, "Zone", zone_name=cfg.domain_name
        )
        self.certificate = acm.Certificate(
            self, "Cert",
            domain_name=cfg.domain_name,
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
        )
