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

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from pep_oracle_infra.config import DeployConfig

# Warmer cadence: under the 5-minute corpus TTL so a refresh is normally absorbed by a
# warm invocation rather than a user's search.
WARM_INTERVAL_MINUTES = Duration.minutes(4)
# Floor for the "warming stopped" alarm: the warmer alone yields 15/hour at a 4-minute
# cadence, so 10 leaves room for a missed tick or a clock-boundary split.
WARM_MIN_INVOCATIONS_PER_HOUR = 10


class PepOracleProdStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: DeployConfig,
        cert_arn: str | None = None,
        web_acl_arn: str | None = None,
        hosted_zone_id: str | None = None,
        hosted_zone_name: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg
        self._cert_arn = cert_arn
        self._web_acl_arn = web_acl_arn
        self._hosted_zone_id = hosted_zone_id
        self._hosted_zone_name = hosted_zone_name

        # --- Data layer: KMS + S3 corpus bucket + DynamoDB OAuth table ---
        self.kms_key = kms.Key(
            self,
            "DataKey",
            description="pep-oracle encryption-at-rest (S3 corpus, DynamoDB, SSM signing key)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.corpus_bucket = s3.Bucket(
            self,
            "CorpusBucket",
            bucket_name=cfg.corpus_bucket_name,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.oauth_table = dynamodb.Table(
            self,
            "OAuthTable",
            table_name=cfg.oauth_table_name,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
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
            partition_key=dynamodb.Attribute(name="family_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # --- Cognito: one-user pool + Hosted-UI domain + confidential app client ---
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            sign_in_aliases=cognito.SignInAliases(email=True),
            self_sign_up_enabled=False,  # single operator-created user
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.user_pool_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=cfg.cognito_domain_prefix),
        )
        self.user_pool_client = self.user_pool.add_client(
            "AppClient",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{cfg.public_url}/oauth/authorize/callback"],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
        )

        # --- Serving Lambda (container) + Function URL + least-privilege IAM ---
        project_root = Path(__file__).resolve().parents[2]

        env = {
            "PEP_ORACLE_SERVE_FROM_ARTIFACT": "1",
            # Only /tmp is writable on Lambda; point the data dir there so any
            # ensure_dirs()/cache path can't crash on the read-only HOME.
            "PEP_ORACLE_DATA_DIR": "/tmp/.pep-oracle",
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
            "PEP_ORACLE_SEMVER": cfg.semver,
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
        self.fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{cfg.compute_region}::foundation-model/{cfg.embed_model}"
                ],
            )
        )
        self.fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{cfg.compute_region}:{self.account}:parameter{cfg.signing_ssm_param}"
                ],
            )
        )

        # Scheduled warmer: at this traffic level every real search lands on a cold
        # container (~8 s wall: INIT + lazy corpus/index load). Invoking the real
        # search path every 4 min keeps one container and its resident corpus warm
        # and absorbs the 5-min corpus TTL refresh, for well under $1/month. The
        # sentinel event is routed by server.handler before Mangum — no public
        # route, no auth surface. Do NOT warm at INIT instead: the corpus must stay
        # lazy so health/discovery cold starts remain cheap (see
        # docs/aws/cold-path-measurement.md).
        warm_rule = events.Rule(
            self,
            "ServeWarmSchedule",
            schedule=events.Schedule.rate(WARM_INTERVAL_MINUTES),
            targets=[
                targets.LambdaFunction(
                    self.fn,
                    event=events.RuleTargetInput.from_object({"pep_oracle_warm": True}),
                )
            ],
        )

        # --- Monitoring / alerting ---
        # Own topic rather than the ingest stack's: the two stacks are deliberately
        # decoupled (see the external-resource imports in ingest_stack) so deploying
        # one never redeploys the other.
        serve_alerts = sns.Topic(self, "ServeAlerts", display_name="pep-oracle serving alerts")
        serve_alerts.add_subscription(subs.EmailSubscription(cfg.alert_email or cfg.allowed_email))

        # 1) The warmer's search raised (broken corpus/index/Bedrock path). server.handler
        #    lets warm-search exceptions propagate precisely so they land here — Mangum
        #    swallows HTTP-request errors into 500s, so this metric is a clean warmer
        #    signal rather than ordinary request noise.
        errors_alarm = cloudwatch.Alarm(
            self,
            "ServeFnErrorsAlarm",
            metric=self.fn.metric_errors(period=Duration.minutes(15), statistic="Sum"),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "pep-oracle serving Lambda raised — most likely the scheduled warmer's "
                "search failed (corpus, prebuilt index, or Bedrock). Users would fall "
                "back to cold searches. Check the ServeFn logs for warm.search."
            ),
        )
        errors_alarm.add_alarm_action(cw_actions.SnsAction(serve_alerts))

        # 2) Warming stopped silently (rule disabled/deleted, or invocations never
        #    arrive). The warmer alone guarantees 60/WARM_INTERVAL invocations an hour
        #    independent of user traffic, so a count well under that means it stalled.
        #    Missing data is itself the failure here, hence BREACHING.
        warm_stopped_alarm = cloudwatch.Alarm(
            self,
            "ServeWarmStoppedAlarm",
            metric=self.fn.metric_invocations(period=Duration.hours(1), statistic="Sum"),
            threshold=WARM_MIN_INVOCATIONS_PER_HOUR,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            alarm_description=(
                f"Fewer than {WARM_MIN_INVOCATIONS_PER_HOUR} serving-Lambda invocations in "
                f"an hour — the {WARM_INTERVAL_MINUTES.to_minutes()}-minute warmer should "
                "alone produce ~"
                f"{60 // WARM_INTERVAL_MINUTES.to_minutes()}. Warming has likely stopped, so "
                "every user search pays the ~6s cold path."
            ),
        )
        warm_stopped_alarm.add_alarm_action(cw_actions.SnsAction(serve_alerts))

        # 3) EventBridge could not deliver to the Lambda at all (e.g. throttled against
        #    the account's concurrency=10) — invisible to both alarms above.
        warm_failed_alarm = cloudwatch.Alarm(
            self,
            "ServeWarmFailedInvocationsAlarm",
            metric=cloudwatch.Metric(
                namespace="AWS/Events",
                metric_name="FailedInvocations",
                dimensions_map={"RuleName": warm_rule.rule_name},
                period=Duration.minutes(15),
                statistic="Sum",
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "EventBridge failed to invoke the serving Lambda for the warm schedule "
                "(e.g. throttling against the account concurrency limit)."
            ),
        )
        warm_failed_alarm.add_alarm_action(cw_actions.SnsAction(serve_alerts))
        self.serve_alerts_topic = serve_alerts

        # HTTP API ($default proxy -> Lambda) instead of a Lambda Function URL: this
        # account blocks public (auth=NONE) function URLs, and AWS_IAM/OAC would sign the
        # Authorization header (colliding with the MCP viewer bearer). An HTTP API passes
        # the bearer through natively; Mangum handles the API Gateway v2 event unchanged.
        # The app's own JWT (/mcp), PKCE (/token) and Cognito (/authorize) checks are the
        # security boundary.
        self.http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            default_integration=apigw_integrations.HttpLambdaIntegration("LambdaProxy", self.fn),
        )
        api_domain = f"{self.http_api.api_id}.execute-api.{cfg.compute_region}.amazonaws.com"

        # --- Public endpoint: CloudFront + Route 53 alias (cert is cross-region) ---
        cert = acm.Certificate.from_certificate_arn(self, "Cert", self._cert_arn)
        zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
            self,
            "Zone",
            hosted_zone_id=self._hosted_zone_id,
            zone_name=self._hosted_zone_name,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    api_domain,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            domain_names=[cfg.domain_name],
            certificate=cert,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            web_acl_id=self._web_acl_arn,
        )

        route53.ARecord(
            self,
            "AliasA",
            zone=zone,
            record_name=cfg.domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            ),
        )
