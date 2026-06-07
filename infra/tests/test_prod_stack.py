"""Template assertions for PepOracleProdStack."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.prod_stack import PepOracleProdStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="pep-oracle-test",
        allowed_email="me@example.com",
    )


def _template() -> Template:
    app = cdk.App()
    stack = PepOracleProdStack(
        app, "Prod", cfg=_cfg(),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        hosted_zone_id="Z123456ABCDEFG",
        hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True, env=ENV,
    )
    return Template.from_stack(stack)


def test_dynamodb_table_matches_store_schema():
    t = _template()
    t.has_resource_properties("AWS::DynamoDB::Table", Match.object_like({
        "TableName": "pep-oracle-oauth",
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
        "GlobalSecondaryIndexes": Match.array_with([
            Match.object_like({
                "IndexName": "family-index",
                "KeySchema": [{"AttributeName": "family_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            })
        ]),
    }))


def test_corpus_bucket_is_private_versioned_encrypted():
    t = _template()
    t.has_resource_properties("AWS::S3::Bucket", Match.object_like({
        "VersioningConfiguration": {"Status": "Enabled"},
        "PublicAccessBlockConfiguration": Match.object_like({
            "BlockPublicAcls": True, "RestrictPublicBuckets": True,
        }),
    }))


def test_kms_key_present():
    t = _template()
    t.resource_count_is("AWS::KMS::Key", 1)


def test_cognito_user_pool_and_domain():
    t = _template()
    t.resource_count_is("AWS::Cognito::UserPool", 1)
    t.has_resource_properties("AWS::Cognito::UserPoolDomain", Match.object_like({
        "Domain": "pep-oracle-test",
    }))


def test_cognito_client_is_confidential_auth_code():
    t = _template()
    t.has_resource_properties("AWS::Cognito::UserPoolClient", Match.object_like({
        "GenerateSecret": True,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": Match.array_with(["openid", "email"]),
        "CallbackURLs": ["https://pep-oracle.iicapn.com/oauth/authorize/callback"],
        "SupportedIdentityProviders": ["COGNITO"],
    }))
