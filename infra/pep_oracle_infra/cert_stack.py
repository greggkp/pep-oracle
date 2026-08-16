"""us-east-1 stack: Route 53 hosted zone for the MCP domain + the CloudFront ACM cert.

CloudFront requires its ACM cert in us-east-1, so the zone+cert live here and the
prod stack (ap-southeast-2) references the cert ARN cross-region.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from pep_oracle_infra.config import DeployConfig


class PepOracleCertStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: DeployConfig, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg

        self.hosted_zone = route53.PublicHostedZone(self, "Zone", zone_name=cfg.domain_name)
        self.certificate = acm.Certificate(
            self,
            "Cert",
            domain_name=cfg.domain_name,
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
        )

        # CloudFront-scoped WAF resources must live in us-east-1, alongside this
        # certificate. Rate rules protect the public, state-changing OAuth routes
        # and the Bedrock-backed MCP search path from cost/availability abuse.
        def rate_rule(name: str, priority: int, limit: int, path: str, positional: str):
            return wafv2.CfnWebACL.RuleProperty(
                name=name,
                priority=priority,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        aggregate_key_type="IP",
                        limit=limit,
                        evaluation_window_sec=300,
                        scope_down_statement=wafv2.CfnWebACL.StatementProperty(
                            byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                                field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(uri_path={}),
                                positional_constraint=positional,
                                search_string=path,
                                text_transformations=[
                                    wafv2.CfnWebACL.TextTransformationProperty(
                                        priority=0, type="NONE"
                                    )
                                ],
                            )
                        ),
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=name,
                    sampled_requests_enabled=True,
                ),
            )

        self.web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="pep-oracle-web-acl",
                sampled_requests_enabled=True,
            ),
            rules=[
                rate_rule("OAuthRegisterRateLimit", 0, 50, "/oauth/register", "STARTS_WITH"),
                rate_rule("OAuthTokenRateLimit", 1, 100, "/oauth/token", "STARTS_WITH"),
                rate_rule("McpRateLimit", 2, 300, "/mcp", "STARTS_WITH"),
            ],
        )
