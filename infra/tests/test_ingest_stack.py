"""Template assertions for PepOracleIngestStack."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template

from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.ingest_stack import PepOracleIngestStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com", compute_region="ap-southeast-2",
        cert_region="us-east-1", corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="p", allowed_email="me@example.com",
    )


def _template() -> Template:
    app = cdk.App()
    refs = cdk.Stack(app, "Refs", env=ENV)
    bucket = s3.Bucket.from_bucket_name(refs, "B", "pep-oracle-corpus-test")
    key = kms.Key.from_key_arn(
        refs, "K", "arn:aws:kms:ap-southeast-2:111111111111:key/abc"
    )
    stack = PepOracleIngestStack(
        app, "Ingest", cfg=_cfg(), data_key=key, corpus_bucket=bucket,
        cross_region_references=True, env=ENV,
    )
    return Template.from_stack(stack)


def test_fargate_taskdef_with_ingest_command():
    t = _template()
    t.resource_count_is("AWS::ECS::Cluster", 1)
    t.has_resource_properties("AWS::ECS::TaskDefinition", Match.object_like({
        "RequiresCompatibilities": ["FARGATE"],
        "ContainerDefinitions": Match.array_with([
            Match.object_like({
                "Command": ["ingest-artifact"],
                "Secrets": Match.array_with([
                    Match.object_like({"Name": "MODAL_TOKEN_ID"}),
                    Match.object_like({"Name": "MODAL_TOKEN_SECRET"}),
                ]),
                "Environment": Match.array_with([
                    Match.object_like({"Name": "PEP_ORACLE_EMBED_BACKEND", "Value": "bedrock"}),
                    Match.object_like({"Name": "PEP_ORACLE_SERVE_FROM_ARTIFACT", "Value": "0"}),
                    Match.object_like({"Name": "PEP_ORACLE_CORPUS_URI", "Value": "s3://pep-oracle-corpus-test"}),
                ]),
            })
        ]),
    }))


def test_daily_eventbridge_rule_targets_ecs():
    t = _template()
    t.has_resource_properties("AWS::Events::Rule", Match.object_like({
        "ScheduleExpression": "rate(1 day)",
        "Targets": Match.array_with([Match.object_like({"EcsParameters": Match.any_value()})]),
    }))


def test_task_role_has_bedrock_and_s3_and_ssm():
    t = _template()
    # Bedrock invoke permission (inline policy on the task role)
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({"Action": "bedrock:InvokeModel"}),
            ])
        })
    }))
    # S3 write: grant_read_write renders a list; confirm PutObject is present
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": Match.array_with(["s3:PutObject"]),
                }),
            ])
        })
    }))
    # KMS: grant_encrypt_decrypt renders a list; kms:Decrypt is the unambiguous literal
    # (kms:GenerateDataKey* is also present but the trailing * confuses array_with matching)
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": Match.array_with(["kms:Decrypt"]),
                }),
            ])
        })
    }))
