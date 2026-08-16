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


def test_cloudfront_waf_rate_limits_sensitive_routes():
    t = _t()
    t.has_resource_properties(
        "AWS::WAFv2::WebACL",
        Match.object_like(
            {
                "Scope": "CLOUDFRONT",
                "DefaultAction": {"Allow": {}},
                "Rules": Match.array_with(
                    [
                        Match.object_like({"Name": "OAuthRegisterRateLimit"}),
                        Match.object_like({"Name": "OAuthTokenRateLimit"}),
                        Match.object_like({"Name": "McpRateLimit"}),
                    ]
                ),
            }
        ),
    )


def test_waf_logs_only_blocked_requests_with_sensitive_fields_redacted():
    t = _t()
    t.has_resource_properties(
        "AWS::Logs::LogGroup",
        Match.object_like(
            {
                "LogGroupName": "aws-waf-logs-pep-oracle-blocked",
                "RetentionInDays": 30,
            }
        ),
    )
    t.has_resource_properties(
        "AWS::WAFv2::LoggingConfiguration",
        Match.object_like(
            {
                "LogDestinationConfigs": Match.any_value(),
                "LoggingFilter": {
                    "DefaultBehavior": "DROP",
                    "Filters": [
                        {
                            "Behavior": "KEEP",
                            "Conditions": [{"ActionCondition": {"Action": "BLOCK"}}],
                            "Requirement": "MEETS_ANY",
                        }
                    ],
                },
                "RedactedFields": Match.array_with(
                    [
                        {"SingleHeader": {"Name": "authorization"}},
                        {"QueryString": {}},
                    ]
                ),
            }
        ),
    )


def test_waf_block_alarm_notifies_operator():
    t = _t()
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "Namespace": "AWS/WAFV2",
                "MetricName": "BlockedRequests",
                "Dimensions": Match.array_with(
                    [
                        {"Name": "Rule", "Value": "ALL"},
                        {"Name": "WebACL", "Value": "pep-oracle-web-acl"},
                    ]
                ),
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "TreatMissingData": "notBreaching",
                "AlarmActions": Match.any_value(),
            }
        ),
    )
    t.has_resource_properties(
        "AWS::SNS::Subscription",
        Match.object_like({"Protocol": "email", "Endpoint": "me@example.com"}),
    )
