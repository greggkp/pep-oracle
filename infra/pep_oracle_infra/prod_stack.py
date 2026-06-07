"""ap-southeast-2 prod stack: data layer, Cognito, Lambda + Function URL, CloudFront,
Route 53 alias. Resources are added in Tasks 4-7.
"""

from __future__ import annotations

from typing import Optional

from aws_cdk import RemovalPolicy
from aws_cdk import Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from pep_oracle_infra.config import DeployConfig


class PepOracleProdStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: DeployConfig,
        cert_arn: Optional[str] = None,
        hosted_zone_id: Optional[str] = None,
        hosted_zone_name: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg
        self._cert_arn = cert_arn
        self._hosted_zone_id = hosted_zone_id
        self._hosted_zone_name = hosted_zone_name

        # Task 4: KMS + S3 corpus bucket + DynamoDB OAuth table
        self.kms_key = kms.Key(
            self, "DataKey",
            description="pep-oracle encryption-at-rest (S3 corpus, DynamoDB, SSM signing key)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.corpus_bucket = s3.Bucket(
            self, "CorpusBucket",
            bucket_name=cfg.corpus_bucket_name,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.oauth_table = dynamodb.Table(
            self, "OAuthTable",
            table_name=cfg.oauth_table_name,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.oauth_table.add_global_secondary_index(
            index_name="family-index",
            partition_key=dynamodb.Attribute(
                name="family_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # Task 5: Cognito user pool + domain + app client
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            sign_in_aliases=cognito.SignInAliases(email=True),
            self_sign_up_enabled=False,  # single operator-created user
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.user_pool_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cfg.cognito_domain_prefix
            ),
        )
        self.user_pool_client = self.user_pool.add_client(
            "AppClient",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{cfg.public_url}/oauth/authorize/callback"],
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            prevent_user_existence_errors=True,
        )

        # Task 6: Lambda (container) + Function URL + IAM
        # Task 7: CloudFront (cert cross-region) + Route 53 alias
