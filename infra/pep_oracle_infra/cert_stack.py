"""us-east-1 stack: Route 53 hosted zone for the MCP domain + the CloudFront ACM cert.

CloudFront requires its ACM cert in us-east-1, so the zone+cert live here and the
prod stack (ap-southeast-2) references the cert ARN cross-region.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
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

        # Retain only blocked-request records. Query strings can contain short-lived
        # OAuth authorization codes and Authorization carries bearer tokens, so both
        # are redacted before delivery. CloudFront-scoped WAF logs and metrics live in
        # us-east-1 with this stack.
        waf_log_group = logs.LogGroup(
            self,
            "WafBlockedRequestsLog",
            log_group_name="aws-waf-logs-pep-oracle-blocked",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        waf_logging = wafv2.CfnLoggingConfiguration(
            self,
            "WafLogging",
            resource_arn=self.web_acl.attr_arn,
            log_destination_configs=[waf_log_group.log_group_arn],
            redacted_fields=[
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "authorization"}
                ),
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(query_string={}),
            ],
            # `logging_filter` is typed as Any in CloudFormation/CDK, so use the
            # exact CloudFormation casing rather than L1 property wrappers (which
            # would synthesize lower-camel keys and be rejected at deploy time).
            logging_filter={
                "DefaultBehavior": "DROP",
                "Filters": [
                    {
                        "Behavior": "KEEP",
                        "Requirement": "MEETS_ANY",
                        "Conditions": [{"ActionCondition": {"Action": "BLOCK"}}],
                    }
                ],
            },
        )
        waf_logging.add_dependency(self.web_acl)

        waf_alerts = sns.Topic(self, "WafAlerts", display_name="pep-oracle WAF alerts")
        waf_alerts.add_subscription(subs.EmailSubscription(cfg.alert_email or cfg.allowed_email))
        blocked_alarm = cloudwatch.Alarm(
            self,
            "WafBlockedRequestsAlarm",
            metric=cloudwatch.Metric(
                namespace="AWS/WAFV2",
                metric_name="BlockedRequests",
                dimensions_map={"WebACL": "pep-oracle-web-acl", "Rule": "ALL"},
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "AWS WAF blocked one or more pep-oracle requests in five minutes. "
                "Inspect the redacted blocked-request log for the matched rate rule."
            ),
        )
        blocked_alarm.add_alarm_action(cw_actions.SnsAction(waf_alerts))
