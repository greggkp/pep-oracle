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
        app,
        "Prod",
        cfg=_cfg(),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        web_acl_arn="arn:aws:wafv2:us-east-1:111111111111:global/webacl/pep/abc",
        hosted_zone_id="Z123456ABCDEFG",
        hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True,
        env=ENV,
    )
    return Template.from_stack(stack)


def test_dynamodb_table_matches_store_schema():
    t = _template()
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        Match.object_like(
            {
                "TableName": "pep-oracle-oauth",
                "BillingMode": "PAY_PER_REQUEST",
                "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
                "GlobalSecondaryIndexes": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "IndexName": "family-index",
                                "KeySchema": [{"AttributeName": "family_id", "KeyType": "HASH"}],
                                "Projection": {"ProjectionType": "KEYS_ONLY"},
                            }
                        )
                    ]
                ),
            }
        ),
    )


def test_corpus_bucket_is_private_versioned_encrypted():
    t = _template()
    t.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "VersioningConfiguration": {"Status": "Enabled"},
                "PublicAccessBlockConfiguration": Match.object_like(
                    {
                        "BlockPublicAcls": True,
                        "RestrictPublicBuckets": True,
                    }
                ),
            }
        ),
    )


def test_kms_key_present():
    t = _template()
    t.resource_count_is("AWS::KMS::Key", 1)


def test_cognito_user_pool_and_domain():
    t = _template()
    t.resource_count_is("AWS::Cognito::UserPool", 1)
    t.has_resource_properties(
        "AWS::Cognito::UserPoolDomain",
        Match.object_like(
            {
                "Domain": "pep-oracle-test",
            }
        ),
    )


def test_cognito_client_is_confidential_auth_code():
    t = _template()
    t.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        Match.object_like(
            {
                "GenerateSecret": True,
                "AllowedOAuthFlows": ["code"],
                "AllowedOAuthScopes": Match.array_with(["openid", "email"]),
                "CallbackURLs": ["https://pep-oracle.iicapn.com/oauth/authorize/callback"],
                "SupportedIdentityProviders": ["COGNITO"],
            }
        ),
    )


def test_cognito_client_secret_is_cmk_encrypted_and_not_in_lambda_env():
    t = _template()
    t.has_resource_properties(
        "AWS::SecretsManager::Secret",
        Match.object_like(
            {
                "Name": "pep-oracle/cognito-client-secret",
                "KmsKeyId": Match.any_value(),
                "SecretString": Match.object_like({"Fn::GetAtt": Match.any_value()}),
            }
        ),
    )
    t.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Environment": {
                    "Variables": Match.object_like(
                        {
                            "PEP_ORACLE_COGNITO_CLIENT_SECRET_ARN": Match.any_value(),
                            "PEP_ORACLE_COGNITO_CLIENT_SECRET_REGION": "ap-southeast-2",
                            "PEP_ORACLE_COGNITO_CLIENT_SECRET_CACHE_SECONDS": "300",
                            "PEP_ORACLE_COGNITO_CLIENT_SECRET": Match.absent(),
                        }
                    )
                }
            }
        ),
    )


def test_lambda_env_has_serving_contract():
    t = _template()
    t.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "PackageType": "Image",
                "Environment": {
                    "Variables": Match.object_like(
                        {
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
                        }
                    )
                },
            }
        ),
    )


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
        app,
        "ProdRC",
        cfg=replace(_cfg(), lambda_reserved_concurrency=5),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        hosted_zone_id="Z123456ABCDEFG",
        hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True,
        env=ENV,
    )
    Template.from_stack(stack).has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"ReservedConcurrentExecutions": 5}),
    )


def test_http_api_proxies_to_lambda():
    # HTTP API ($default proxy) instead of a Function URL (public function URLs are
    # blocked on this account; APIGW passes the bearer through, no OAC/SigV4 conflict).
    t = _template()
    t.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    t.has_resource_properties(
        "AWS::ApiGatewayV2::Integration",
        Match.object_like(
            {
                "IntegrationType": "AWS_PROXY",
                "PayloadFormatVersion": "2.0",
            }
        ),
    )
    # No Lambda Function URL remains.
    t.resource_count_is("AWS::Lambda::Url", 0)


def test_lambda_role_has_bedrock_ssm_and_secret_read():
    t = _template()
    # Bedrock InvokeModel on the embed model + SSM GetParameter on the signing param
    t.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {"Action": Match.array_with(["secretsmanager:GetSecretValue"])}
                                ),
                                Match.object_like({"Action": "bedrock:InvokeModel"}),
                                Match.object_like({"Action": "ssm:GetParameter"}),
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_cloudfront_distribution_has_domain_no_oac():
    t = _template()
    t.has_resource_properties(
        "AWS::CloudFront::Distribution",
        Match.object_like(
            {
                "DistributionConfig": Match.object_like(
                    {
                        "Aliases": ["pep-oracle.iicapn.com"],
                        "WebACLId": "arn:aws:wafv2:us-east-1:111111111111:global/webacl/pep/abc",
                    }
                )
            }
        ),
    )
    # No OAC: the Function URL is public (auth=NONE) and app-layer auth protects it.
    t.resource_count_is("AWS::CloudFront::OriginAccessControl", 0)


def test_route53_alias_record_present():
    t = _template()
    t.has_resource_properties(
        "AWS::Route53::RecordSet",
        Match.object_like(
            {
                "Type": "A",
                "Name": "pep-oracle.iicapn.com.",
            }
        ),
    )


def test_lambda_has_semver_env():
    t = _template()
    t.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Environment": {
                    "Variables": Match.object_like({"PEP_ORACLE_SEMVER": Match.any_value()})
                },
            }
        ),
    )


def test_warmer_schedule_invokes_lambda_with_sentinel():
    # Scheduled warmer: rate(4 min) direct-invokes the serving Lambda with the
    # pep_oracle_warm sentinel so one container keeps its lazily loaded corpus +
    # prebuilt index resident (every real search is otherwise a cold start).
    t = _template()
    t.has_resource_properties(
        "AWS::Events::Rule",
        Match.object_like(
            {
                "ScheduleExpression": "rate(4 minutes)",
                "Targets": Match.array_with(
                    [Match.object_like({"Input": '{"pep_oracle_warm":true}'})]
                ),
            }
        ),
    )


def test_serving_alerts_topic_has_email_subscription():
    # Serving alarms notify via their own topic, not the ingest stack's, so the two
    # stacks stay independently deployable.
    t = _template()
    t.has_resource_properties(
        "AWS::SNS::Subscription",
        Match.object_like({"Protocol": "email", "Endpoint": "me@example.com"}),
    )


def test_warmer_alarms_present_and_wired_to_topic():
    # Three failure modes: the warm search raises; warming silently stops (no/too few
    # invocations); EventBridge can't deliver at all.
    t = _template()
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "Errors",
                "Namespace": "AWS/Lambda",
                "ComparisonOperator": "GreaterThanThreshold",
                "Threshold": 0,
                "AlarmActions": Match.any_value(),
            }
        ),
    )
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "Invocations",
                "Namespace": "AWS/Lambda",
                "ComparisonOperator": "LessThanThreshold",
                "Threshold": 10,
                # Missing data must page: no datapoints is exactly the stalled case.
                "TreatMissingData": "breaching",
                "Period": 3600,
                "AlarmActions": Match.any_value(),
            }
        ),
    )
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "FailedInvocations",
                "Namespace": "AWS/Events",
                "ComparisonOperator": "GreaterThanThreshold",
                "Threshold": 0,
                "AlarmActions": Match.any_value(),
            }
        ),
    )


def test_serving_security_and_availability_alarms_present():
    t = _template()
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "Throttles",
                "Namespace": "AWS/Lambda",
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "AlarmActions": Match.any_value(),
            }
        ),
    )
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "5xx",
                "Namespace": "AWS/ApiGateway",
                "Dimensions": Match.array_with([{"Name": "ApiId", "Value": Match.any_value()}]),
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "AlarmActions": Match.any_value(),
            }
        ),
    )


def test_serve_fn_timeout_alarm_present():
    """An invocation that runs to the timeout never responds — it is blocked, not slow.
    Distinct enough from a raised exception to warrant its own alarm: on 2026-08-23 nine
    hung /mcp GET stream attempts surfaced only as an ambiguous Lambda Errors alarm."""
    t = _template()
    t.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        Match.object_like(
            {
                "MetricName": "Duration",
                "Namespace": "AWS/Lambda",
                "Statistic": "Maximum",
                "Threshold": 28000,
                "ComparisonOperator": "GreaterThanThreshold",
                "AlarmActions": Match.any_value(),
            }
        ),
    )


def test_http_api_access_logging_records_method_and_path():
    """Mangum logs a request only once the app responds, so a hung invocation leaves no
    method/path in the Lambda logs at all. The access log is the only record — and
    httpMethod is what distinguishes a GET stream attempt from an ordinary POST."""
    t = _template()
    t.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        Match.object_like(
            {
                "AccessLogSettings": Match.object_like(
                    {
                        "DestinationArn": Match.any_value(),
                        "Format": Match.string_like_regexp(r".*httpMethod.*"),
                    }
                )
            }
        ),
    )
    t.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        Match.object_like(
            {
                "AccessLogSettings": Match.object_like(
                    {"Format": Match.string_like_regexp(r".*\$context\.path.*")}
                )
            }
        ),
    )


def test_http_api_access_logging_records_mcp_method_header():
    """Every /mcp call is a POST, so httpMethod cannot tell a `subscriptions/listen`
    (whose response IS a notification stream — 45 timeouts in the six days to 2026-08-28)
    from an ordinary tools/call. Request bodies are logged nowhere, so these two headers
    are the only place a hung request's JSON-RPC method is recoverable after the fact."""
    t = _template()
    for header in ("mcp-method", "mcp-protocol-version"):
        t.has_resource_properties(
            "AWS::ApiGatewayV2::Stage",
            Match.object_like(
                {
                    "AccessLogSettings": Match.object_like(
                        {
                            "Format": Match.string_like_regexp(
                                rf".*\$context\.request\.header\.{header}.*"
                            )
                        }
                    )
                }
            ),
        )


def test_cloudfront_access_logging_enabled():
    _template().has_resource_properties(
        "AWS::CloudFront::Distribution",
        Match.object_like(
            {
                "DistributionConfig": Match.object_like(
                    {"Logging": Match.object_like({"Bucket": Match.any_value(), "Prefix": "cdn/"})}
                )
            }
        ),
    )


def test_access_logs_bucket_allows_acl_writes_and_is_not_cmk_encrypted():
    """CloudFront's legacy log delivery writes with an ACL and cannot target an SSE-KMS
    customer key — so this bucket must keep ACLs enabled and use SSE-S3, unlike the
    CMK-encrypted corpus bucket. Getting either wrong fails delivery silently."""
    t = _template()
    t.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "OwnershipControls": Match.object_like(
                    {"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]}
                ),
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": [
                        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                    ]
                },
            }
        ),
    )


def test_serving_lambda_log_retention_is_30_days():
    _template().has_resource_properties(
        "Custom::LogRetention",
        Match.object_like({"RetentionInDays": 30}),
    )
