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


def test_lambda_env_has_serving_contract():
    t = _template()
    t.has_resource_properties("AWS::Lambda::Function", Match.object_like({
        "PackageType": "Image",
        "Environment": {"Variables": Match.object_like({
            "PEP_ORACLE_SERVE_FROM_ARTIFACT": "1",
            "PEP_ORACLE_EMBED_BACKEND": "bedrock",
            "PEP_ORACLE_EMBED_MODEL": "amazon.titan-embed-text-v2:0",
            "PEP_ORACLE_OAUTH_STORE": "dynamodb",
            "PEP_ORACLE_OAUTH_DDB_TABLE": "pep-oracle-oauth",
            "PEP_ORACLE_OAUTH_SIGNING_BACKEND": "ssm",
            "PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM": "/pep-oracle/oauth-signing-key",
            "PEP_ORACLE_AUTHORIZE_GATE": "cognito",
            "PEP_ORACLE_PUBLIC_URL": "https://pep-oracle.iicapn.com",
            "PEP_ORACLE_CORPUS_URI": "s3://pep-oracle-corpus-test",
            "PEP_ORACLE_GIT_SHA": "unknown",
        })},
    }))


def test_lambda_reserved_concurrency_default_off_and_configurable():
    from dataclasses import replace

    # Default: no reservation (the account's default-10 concurrency can't support one).
    _template().resource_properties_count_is(
        "AWS::Lambda::Function",
        Match.object_like({"ReservedConcurrentExecutions": Match.any_value()}),
        0,
    )

    # Configured via context: applied to the serving function.
    app = cdk.App()
    stack = PepOracleProdStack(
        app, "ProdRC", cfg=replace(_cfg(), lambda_reserved_concurrency=5),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        hosted_zone_id="Z123456ABCDEFG", hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True, env=ENV,
    )
    Template.from_stack(stack).has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"ReservedConcurrentExecutions": 5}),
    )


def test_function_url_is_iam_auth():
    t = _template()
    t.has_resource_properties("AWS::Lambda::Url", Match.object_like({
        "AuthType": "AWS_IAM",
    }))


def test_lambda_role_has_bedrock_and_ssm():
    t = _template()
    # Bedrock InvokeModel on the embed model + SSM GetParameter on the signing param
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({"Action": "bedrock:InvokeModel"}),
                Match.object_like({"Action": "ssm:GetParameter"}),
            ])
        })
    }))


def test_cloudfront_distribution_has_domain_and_oac_origin():
    t = _template()
    t.has_resource_properties("AWS::CloudFront::Distribution", Match.object_like({
        "DistributionConfig": Match.object_like({
            "Aliases": ["pep-oracle.iicapn.com"],
        })
    }))
    # OAC is created for the Function URL origin
    t.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_route53_alias_record_present():
    t = _template()
    t.has_resource_properties("AWS::Route53::RecordSet", Match.object_like({
        "Type": "A",
        "Name": "pep-oracle.iicapn.com.",
    }))
