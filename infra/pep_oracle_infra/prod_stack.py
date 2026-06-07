"""ap-southeast-2 prod stack for the pep-oracle MCP serving endpoint.

Owns: a KMS key; a private/versioned/KMS-encrypted S3 corpus bucket; the DynamoDB
OAuth table (schema matching oauth_store.DynamoDbStore); a one-user Cognito pool +
Hosted-UI domain + confidential app client; the container serving Lambda (FastAPI +
Mangum) behind a Function URL (AWS_IAM) fronted by CloudFront + OAC; a Route 53
A-alias; and least-privilege IAM. The CloudFront ACM cert lives in us-east-1
(PepOracleCertStack) and is referenced here cross-region.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aws_cdk import Duration
from aws_cdk import RemovalPolicy
from aws_cdk import Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
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

        # --- Data layer: KMS + S3 corpus bucket + DynamoDB OAuth table ---
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
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.oauth_table.add_global_secondary_index(
            index_name="family-index",
            partition_key=dynamodb.Attribute(
                name="family_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # --- Cognito: one-user pool + Hosted-UI domain + confidential app client ---
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

        # --- Serving Lambda (container) + Function URL + least-privilege IAM ---
        project_root = Path(__file__).resolve().parents[2]

        env = {
            "PEP_ORACLE_SERVE_FROM_ARTIFACT": "1",
            "PEP_ORACLE_EMBED_BACKEND": "bedrock",
            "PEP_ORACLE_BEDROCK_REGION": cfg.compute_region,
            "PEP_ORACLE_EMBED_MODEL": cfg.embed_model,
            "PEP_ORACLE_EMBED_DIMS": cfg.embed_dims,
            "PEP_ORACLE_CORPUS_URI": f"s3://{cfg.corpus_bucket_name}",
            "PEP_ORACLE_OAUTH_STORE": "dynamodb",
            "PEP_ORACLE_OAUTH_DDB_TABLE": cfg.oauth_table_name,
            "PEP_ORACLE_OAUTH_DDB_REGION": cfg.compute_region,
            "PEP_ORACLE_OAUTH_SIGNING_BACKEND": "ssm",
            "PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM": cfg.signing_ssm_param,
            "PEP_ORACLE_OAUTH_SIGNING_SSM_REGION": cfg.compute_region,
            "PEP_ORACLE_AUTHORIZE_GATE": "cognito",
            "PEP_ORACLE_COGNITO_DOMAIN": (
                f"https://{cfg.cognito_domain_prefix}.auth.{cfg.compute_region}.amazoncognito.com"
            ),
            "PEP_ORACLE_COGNITO_CLIENT_ID": self.user_pool_client.user_pool_client_id,
            "PEP_ORACLE_COGNITO_CLIENT_SECRET": (
                self.user_pool_client.user_pool_client_secret.unsafe_unwrap()
            ),
            "PEP_ORACLE_COGNITO_USER_POOL_ID": self.user_pool.user_pool_id,
            "PEP_ORACLE_COGNITO_REGION": cfg.compute_region,
            "PEP_ORACLE_COGNITO_ALLOWED_EMAILS": cfg.allowed_email,
            "PEP_ORACLE_PUBLIC_URL": cfg.public_url,
            # Code provenance for GET /version; supply at deploy via `-c git_sha=...`
            # (defaults to "unknown" until the Phase 4 pipeline bakes it).
            "PEP_ORACLE_GIT_SHA": cfg.git_sha,
        }

        fn_kwargs = dict(
            code=lambda_.DockerImageCode.from_image_asset(str(project_root)),
            memory_size=2048,
            timeout=Duration.seconds(30),
            environment=env,
        )
        # Reserving concurrency requires the account's unreserved pool to stay >= 10;
        # the default-10 account limit can't support any reservation, so default off.
        if cfg.lambda_reserved_concurrency:
            fn_kwargs["reserved_concurrent_executions"] = cfg.lambda_reserved_concurrency
        self.fn = lambda_.DockerImageFunction(self, "ServeFn", **fn_kwargs)

        # Least-privilege grants
        self.corpus_bucket.grant_read(self.fn)
        self.oauth_table.grant_read_write_data(self.fn)
        self.kms_key.grant_decrypt(self.fn)  # SSM SecureString + S3/DDB CMK reads
        self.fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{cfg.compute_region}::foundation-model/{cfg.embed_model}"
            ],
        ))
        self.fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{cfg.compute_region}:{self.account}:parameter{cfg.signing_ssm_param}"
            ],
        ))

        # auth=NONE (not AWS_IAM/OAC): OAC signs the Authorization header with SigV4,
        # which collides with the MCP viewer bearer token that must pass through on the
        # same header. The app's own JWT (/mcp), PKCE (/token) and Cognito (/authorize)
        # checks are the security boundary; CloudFront forwards the bearer unchanged.
        self.fn_url = self.fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE
        )

        # --- Public endpoint: CloudFront + Route 53 alias (cert is cross-region) ---
        cert = acm.Certificate.from_certificate_arn(self, "Cert", self._cert_arn)
        zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
            self, "Zone",
            hosted_zone_id=self._hosted_zone_id,
            zone_name=self._hosted_zone_name,
        )

        self.distribution = cloudfront.Distribution(
            self, "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin(self.fn_url),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            domain_names=[cfg.domain_name],
            certificate=cert,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        route53.ARecord(
            self, "AliasA",
            zone=zone,
            record_name=cfg.domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            ),
        )
