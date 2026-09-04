"""Hibernation contract: `hibernate=true` deploys data + DNS only, and restores.

The bill for this deployment is almost entirely fixed per-resource charges, not
usage, so hibernation has to actually remove resources — chiefly the WAF WebACL
(~$7.85/month whether or not a request reaches it). What it must NOT remove is
the data layer: those resources are RETAIN with fixed physical names, so dropping
them from a template orphans them and the next deploy fails re-creating them.
These tests pin both halves of that: what disappears, and what must not.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.ingest_stack import PepOracleIngestStack
from pep_oracle_infra.prod_stack import PepOracleProdStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")
CERT_ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _cfg(*, hibernate: bool) -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="pep-oracle-test",
        allowed_email="me@example.com",
        data_key_id="abc-123",
        hibernate=hibernate,
    )


def _prod(
    *, hibernate: bool, web_acl_arn: str | None = None
) -> tuple[PepOracleProdStack, Template]:
    app = cdk.App()
    stack = PepOracleProdStack(
        app,
        "Prod",
        cfg=_cfg(hibernate=hibernate),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        web_acl_arn=web_acl_arn,
        hosted_zone_id="Z123456ABCDEFG",
        hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True,
        env=ENV,
    )
    return stack, Template.from_stack(stack)


def _cert(*, hibernate: bool) -> tuple[PepOracleCertStack, Template]:
    app = cdk.App()
    stack = PepOracleCertStack(
        app, "Cert", cfg=_cfg(hibernate=hibernate), cross_region_references=True, env=CERT_ENV
    )
    return stack, Template.from_stack(stack)


def _ingest(*, hibernate: bool) -> Template:
    app = cdk.App()
    return Template.from_stack(
        PepOracleIngestStack(app, "Ingest", cfg=_cfg(hibernate=hibernate), env=ENV)
    )


# --- config parsing -------------------------------------------------------------


class _Node:
    def __init__(self, values: dict):
        self._values = values

    def try_get_context(self, key):
        return self._values.get(key)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),  # absent -> serving stays on
        (True, True),  # cdk.json supplies a real JSON bool
        (False, False),
        ("true", True),  # -c hibernate=true always supplies a string
        ("True", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("", False),
    ],
)
def test_hibernate_context_parsing(raw, expected):
    values = {"allowed_email": "me@example.com"}
    if raw is not None:
        values["hibernate"] = raw
    assert DeployConfig.from_node(_Node(values)).hibernate is expected


# --- cert stack: the WAF is the line item -------------------------------------


def test_hibernated_cert_stack_drops_the_webacl():
    stack, t = _cert(hibernate=True)
    t.resource_count_is("AWS::WAFv2::WebACL", 0)
    t.resource_count_is("AWS::WAFv2::LoggingConfiguration", 0)
    assert stack.web_acl is None


def test_hibernated_cert_stack_keeps_dns_cert_and_free_placeholders():
    # Zone + cert stay so restore needs no NS re-delegation at the registrar and no
    # ACM re-validation. The log group has a fixed name and RETAIN: dropping it would
    # orphan it and collide on restore.
    _, t = _cert(hibernate=True)
    t.resource_count_is("AWS::Route53::HostedZone", 1)
    t.resource_count_is("AWS::CertificateManager::Certificate", 1)
    t.has_resource_properties(
        "AWS::Logs::LogGroup",
        Match.object_like({"LogGroupName": "aws-waf-logs-pep-oracle-blocked"}),
    )
    t.resource_count_is("AWS::SNS::Topic", 1)


def test_active_cert_stack_still_has_the_webacl():
    stack, t = _cert(hibernate=False)
    t.resource_count_is("AWS::WAFv2::WebACL", 1)
    t.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)
    assert stack.web_acl is not None


# --- prod stack: the public surface goes, the data stays ------------------------


def _container_functions(t: Template) -> list[dict]:
    # The serving Lambda is the only container (PackageType: Image) function here. A
    # zip-packaged one always survives hibernation: CDK's AwsCustomResource singleton
    # reads the generated Cognito app-client secret at deploy time. It is free and
    # never invoked at rest, so match on the image instead of the type.
    return [
        body
        for body in t.find_resources("AWS::Lambda::Function").values()
        if body["Properties"].get("PackageType") == "Image"
    ]


def test_hibernated_prod_stack_has_no_public_surface():
    stack, t = _prod(hibernate=True)
    assert _container_functions(t) == []
    for resource in (
        "AWS::ApiGatewayV2::Api",
        "AWS::CloudFront::Distribution",
        "AWS::Route53::RecordSet",
        "AWS::Events::Rule",  # the 4-minute warmer
        "AWS::CloudWatch::Alarm",
    ):
        t.resource_count_is(resource, 0)
    assert stack.fn is None
    assert stack.http_api is None
    assert stack.distribution is None


def test_hibernated_prod_stack_keeps_every_retained_data_resource():
    # This is the restore contract. Each of these is RETAIN with a fixed physical
    # name (or holds the corpus/tokens), so it must stay in the template — a
    # `cdk destroy` here is what makes the shutdown a one-way door.
    _, t = _prod(hibernate=True)
    t.has_resource_properties(
        "AWS::S3::Bucket", Match.object_like({"BucketName": "pep-oracle-corpus-test"})
    )
    t.has_resource_properties(
        "AWS::DynamoDB::Table", Match.object_like({"TableName": "pep-oracle-oauth"})
    )
    t.has_resource_properties(
        "AWS::SecretsManager::Secret",
        Match.object_like({"Name": "pep-oracle/cognito-client-secret"}),
    )
    t.resource_count_is("AWS::KMS::Key", 1)
    t.resource_count_is("AWS::Cognito::UserPool", 1)
    t.resource_count_is("AWS::Cognito::UserPoolDomain", 1)
    t.resource_count_is("AWS::Cognito::UserPoolClient", 1)
    for resource in (
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::KMS::Key",
        "AWS::Cognito::UserPool",
        "AWS::SecretsManager::Secret",
    ):
        for body in t.find_resources(resource).values():
            assert body["DeletionPolicy"] == "Retain"


def test_hibernated_prod_stack_keeps_the_alerts_topic_subscribed():
    # Free, and it saves re-confirming the subscription email on restore.
    _, t = _prod(hibernate=True)
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.has_resource_properties(
        "AWS::SNS::Subscription",
        Match.object_like({"Protocol": "email", "Endpoint": "me@example.com"}),
    )


def test_active_prod_stack_still_serves():
    stack, t = _prod(hibernate=False)
    assert len(_container_functions(t)) == 1
    t.resource_count_is("AWS::CloudFront::Distribution", 1)
    t.resource_count_is("AWS::Route53::RecordSet", 1)
    assert stack.fn is not None


def test_active_prod_stack_synthesizes_without_a_webacl():
    # app.py passes web_acl_arn=None whenever the cert stack is hibernated; a
    # half-restored deploy (serving back, WAF not yet) must still synthesize.
    _, t = _prod(hibernate=False, web_acl_arn=None)
    for body in t.find_resources("AWS::CloudFront::Distribution").values():
        assert "WebACLId" not in body["Properties"]["DistributionConfig"]


# --- ingest stack: schedules off, everything else at rest -----------------------


def test_hibernated_ingest_schedules_are_disabled():
    t = _ingest(hibernate=True)
    states = [
        body["Properties"]["State"]
        for body in t.find_resources("AWS::Events::Rule").values()
        if "ScheduleExpression" in body["Properties"]
    ]
    assert states == ["DISABLED", "DISABLED"]


def test_hibernated_ingest_keeps_the_task_definition():
    # The cluster, VPC (no NAT) and task definition are free at rest; only a run costs
    # money, on AWS and on Modal. Keeping them makes restore a schedule re-enable.
    t = _ingest(hibernate=True)
    t.resource_count_is("AWS::ECS::TaskDefinition", 1)
    t.resource_count_is("AWS::ECS::Cluster", 1)


def test_hibernated_ingest_allows_missing_replacement_data_key():
    app = cdk.App()
    cfg = _cfg(hibernate=True)
    cfg = DeployConfig(**{**cfg.__dict__, "data_key_id": ""})
    t = Template.from_stack(PepOracleIngestStack(app, "Ingest", cfg=cfg, env=ENV))
    assert "kms:" not in str(t.to_json())


def test_active_ingest_requires_replacement_data_key():
    app = cdk.App()
    cfg = _cfg(hibernate=False)
    cfg = DeployConfig(**{**cfg.__dict__, "data_key_id": ""})
    with pytest.raises(ValueError, match="data_key_id is required"):
        PepOracleIngestStack(app, "Ingest", cfg=cfg, env=ENV)


def test_active_ingest_schedules_are_enabled():
    t = _ingest(hibernate=False)
    states = [
        body["Properties"].get("State", "ENABLED")
        for body in t.find_resources("AWS::Events::Rule").values()
        if "ScheduleExpression" in body["Properties"]
    ]
    assert states == ["ENABLED", "ENABLED"]
