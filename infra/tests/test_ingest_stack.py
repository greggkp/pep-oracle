"""Template assertions for PepOracleIngestStack."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.ingest_stack import PepOracleIngestStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="p",
        allowed_email="me@example.com",
        data_key_id="abc-123",
    )


def _template() -> Template:
    # The stack imports the corpus bucket + data key as external resources from cfg
    # (by name / by ARN), so it needs no cross-stack constructs passed in.
    app = cdk.App()
    stack = PepOracleIngestStack(app, "Ingest", cfg=_cfg(), env=ENV)
    return Template.from_stack(stack)


def test_fargate_taskdef_with_ingest_command():
    t = _template()
    t.resource_count_is("AWS::ECS::Cluster", 1)
    t.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        Match.object_like(
            {
                "RequiresCompatibilities": ["FARGATE"],
                "ContainerDefinitions": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Command": ["ingest-artifact"],
                                "Secrets": Match.array_with(
                                    [
                                        Match.object_like({"Name": "MODAL_TOKEN_ID"}),
                                        Match.object_like({"Name": "MODAL_TOKEN_SECRET"}),
                                    ]
                                ),
                                "Environment": Match.array_with(
                                    [
                                        Match.object_like(
                                            {"Name": "PEP_ORACLE_EMBED_BACKEND", "Value": "bedrock"}
                                        ),
                                        Match.object_like(
                                            {"Name": "PEP_ORACLE_SERVE_FROM_ARTIFACT", "Value": "0"}
                                        ),
                                        Match.object_like(
                                            {
                                                "Name": "PEP_ORACLE_CORPUS_URI",
                                                "Value": "s3://pep-oracle-corpus-test",
                                            }
                                        ),
                                    ]
                                ),
                            }
                        )
                    ]
                ),
            }
        ),
    )


def test_daily_eventbridge_rule_targets_ecs():
    t = _template()
    t.has_resource_properties(
        "AWS::Events::Rule",
        Match.object_like(
            {
                "ScheduleExpression": "rate(1 day)",
                "Targets": Match.array_with(
                    [Match.object_like({"EcsParameters": Match.any_value()})]
                ),
            }
        ),
    )


def test_task_role_has_bedrock_and_s3_and_ssm():
    t = _template()
    # Bedrock invoke permission (inline policy on the task role)
    t.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like({"Action": "bedrock:InvokeModel"}),
                            ]
                        )
                    }
                )
            }
        ),
    )
    # S3 write: grant_read_write renders a list; confirm PutObject is present
    t.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": Match.array_with(["s3:PutObject"]),
                                    }
                                ),
                            ]
                        )
                    }
                )
            }
        ),
    )
    # KMS: grant_encrypt_decrypt renders a list; kms:Decrypt is the unambiguous literal
    # (kms:GenerateDataKey* is also present but the trailing * confuses array_with matching)
    t.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": Match.array_with(["kms:Decrypt"]),
                                    }
                                ),
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_sns_topic_emails_the_alert_address():
    t = _template()
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.has_resource_properties(
        "AWS::SNS::Subscription",
        Match.object_like(
            {
                "Protocol": "email",
                "Endpoint": "me@example.com",  # falls back to allowed_email when alert_email unset
            }
        ),
    )


def test_task_failure_rule_notifies_sns():
    t = _template()
    t.has_resource_properties(
        "AWS::Events::Rule",
        Match.object_like(
            {
                "EventPattern": Match.object_like(
                    {
                        "source": ["aws.ecs"],
                        "detail-type": ["ECS Task State Change"],
                        "detail": Match.object_like(
                            {
                                "lastStatus": ["STOPPED"],
                                "containers": {"exitCode": [{"anything-but": 0}]},
                            }
                        ),
                    }
                ),
                "Targets": Match.array_with(
                    [
                        Match.object_like(
                            {"Arn": {"Ref": Match.string_like_regexp("IngestAlerts.*")}}
                        ),
                    ]
                ),
            }
        ),
    )


def test_daily_target_has_retry_and_dlq():
    t = _template()
    # SQS DLQ for failed launches.
    t.resource_count_is("AWS::SQS::Queue", 1)
    # The scheduled rule's ECS target carries a retry policy + dead-letter config.
    t.has_resource_properties(
        "AWS::Events::Rule",
        Match.object_like(
            {
                "ScheduleExpression": "rate(1 day)",
                "Targets": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "RetryPolicy": Match.object_like({"MaximumRetryAttempts": 2}),
                                "DeadLetterConfig": Match.any_value(),
                            }
                        )
                    ]
                ),
            }
        ),
    )


def test_stale_corpus_lambda_and_alarm():
    t = _template()
    t.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Handler": "index.handler",
                "Runtime": "python3.12",
                "Environment": Match.object_like(
                    {
                        "Variables": Match.object_like({"CORPUS_BUCKET": "pep-oracle-corpus-test"}),
                    }
                ),
            }
        ),
    )
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "CorpusAgeHours",
                "Namespace": "PepOracle/Ingest",
                "Threshold": 240,
                "ComparisonOperator": "GreaterThanThreshold",
            }
        ),
    )


def test_alarms_action_to_sns():
    t = _template()
    # Both the stale-corpus and DLQ alarms wire their AlarmActions to the SNS topic.
    t.resource_count_is("AWS::CloudWatch::Alarm", 2)
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "AlarmActions": Match.array_with(
                    [
                        Match.object_like({"Ref": Match.string_like_regexp("IngestAlerts.*")}),
                    ]
                ),
            }
        ),
    )


def test_grants_use_imported_resources_not_cross_stack_exports():
    """Decoupling guarantee: the corpus bucket + data key are imported as external
    resources, so the grants reference LITERAL ARNs — never an Fn::ImportValue from
    PepOracleProdStack. This is what keeps a deploy from redeploying the serving Lambda."""
    t = _template()
    # No cross-stack imports anywhere in the synthesized template.
    assert "Fn::ImportValue" not in json.dumps(t.to_json())
    # The KMS grant targets the literal key ARN built from the stack env + data_key_id.
    t.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Action": Match.array_with(["kms:Decrypt"]),
                                        "Resource": "arn:aws:kms:ap-southeast-2:111111111111:key/abc-123",
                                    }
                                ),
                            ]
                        )
                    }
                )
            }
        ),
    )
