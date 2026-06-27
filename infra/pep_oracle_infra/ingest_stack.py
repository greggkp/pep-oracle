"""ap-southeast-2 ingestion stack (Phase 3): a daily EventBridge rule runs a scale-to-zero
Fargate task that incrementally ingests new episodes and publishes a new corpus version to
S3. Modal does the GPU transcribe/diarize; this task orchestrates + Bedrock-embeds.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from pep_oracle_infra.config import DeployConfig

# SSM SecureString params holding the Modal credentials (created out-of-band; see runbook).
MODAL_TOKEN_ID_PARAM = "/pep-oracle/modal-token-id"
MODAL_TOKEN_SECRET_PARAM = "/pep-oracle/modal-token-secret"

# CloudWatch namespace for the corpus-freshness metric the stale-check Lambda emits.
METRIC_NAMESPACE = "PepOracle/Ingest"
# Alarm if no new corpus version has been published in this many hours. The podcast
# publishes ~weekly, so a daily/28h threshold would false-positive on every normal
# gap; 10 days clears a normal week (plus slack) but catches a genuinely stalled feed.
CORPUS_STALE_THRESHOLD_HOURS = 240

# Inline stale-check Lambda: read corpus/current.json -> the version manifest's
# built_at -> publish the corpus age (hours) as a CloudWatch metric. A CloudWatch
# alarm on that metric (below) emails when the corpus goes stale.
_STALE_CHECK_CODE = '''
import datetime
import json
import os
import urllib.parse

import boto3

_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")
BUCKET = os.environ["CORPUS_BUCKET"]
NAMESPACE = os.environ["METRIC_NAMESPACE"]


def handler(event, context):
    cur = json.loads(
        _s3.get_object(Bucket=BUCKET, Key="corpus/current.json")["Body"].read()
    )
    parsed = urllib.parse.urlparse(cur["manifest_url"])  # s3://bucket/corpus/vNNNN.manifest.json
    manifest = json.loads(
        _s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"].read()
    )
    built = datetime.datetime.fromisoformat(manifest["built_at"].replace("Z", "+00:00"))
    if built.tzinfo is None:
        built = built.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    age_hours = (now - built).total_seconds() / 3600.0
    _cw.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[{"MetricName": "CorpusAgeHours", "Value": age_hours, "Unit": "Count"}],
    )
    return {"version": cur.get("version"), "age_hours": age_hours}
'''


class PepOracleIngestStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: DeployConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, cid, **kwargs)

        # Import the corpus bucket + data key as EXTERNAL resources (by name / by ARN
        # built from this stack's env) rather than consuming PepOracleProdStack's
        # constructs. Grants on imported resources are identity-only — no cross-stack
        # export — so deploying this stack never pulls PepOracleProdStack into the
        # deploy set (which would rebuild + redeploy the live serving Lambda).
        corpus_bucket = s3.Bucket.from_bucket_name(
            self, "CorpusBucket", cfg.corpus_bucket_name
        )
        data_key = kms.Key.from_key_arn(
            self, "DataKey",
            f"arn:aws:kms:{self.region}:{self.account}:key/{cfg.data_key_id}",
        )

        # Minimal VPC: 1 AZ, a public subnet, no NAT (scale-to-zero, public egress).
        vpc = ec2.Vpc(
            self, "IngestVpc",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )
        cluster = ecs.Cluster(self, "IngestCluster", vpc=vpc)

        task_def = ecs.FargateTaskDefinition(
            self, "IngestTask", cpu=1024, memory_limit_mib=4096
        )

        project_root = Path(__file__).resolve().parents[2]

        modal_id = ssm.StringParameter.from_secure_string_parameter_attributes(
            self, "ModalIdParam", parameter_name=MODAL_TOKEN_ID_PARAM
        )
        modal_secret = ssm.StringParameter.from_secure_string_parameter_attributes(
            self, "ModalSecretParam", parameter_name=MODAL_TOKEN_SECRET_PARAM
        )

        task_def.add_container(
            "ingest",
            image=ecs.ContainerImage.from_asset(str(project_root), file="Dockerfile.ingest"),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="ingest",
                log_retention=logs.RetentionDays.ONE_MONTH,
            ),
            command=["ingest-artifact"],
            environment={
                "PEP_ORACLE_EMBED_BACKEND": "bedrock",
                "PEP_ORACLE_SERVE_FROM_ARTIFACT": "0",
                "PEP_ORACLE_BEDROCK_REGION": cfg.compute_region,
                "PEP_ORACLE_EMBED_MODEL": cfg.embed_model,
                "PEP_ORACLE_EMBED_DIMS": cfg.embed_dims,
                "PEP_ORACLE_CORPUS_URI": f"s3://{cfg.corpus_bucket_name}",
                "PEP_ORACLE_DATA_DIR": "/tmp/pep-oracle",
                "PEP_ORACLE_GIT_SHA": cfg.git_sha,
            },
            secrets={
                "MODAL_TOKEN_ID": ecs.Secret.from_ssm_parameter(modal_id),
                "MODAL_TOKEN_SECRET": ecs.Secret.from_ssm_parameter(modal_secret),
            },
        )

        # Least-privilege task role.
        role = task_def.task_role
        corpus_bucket.grant_read_write(role)
        data_key.grant_encrypt_decrypt(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{cfg.compute_region}::foundation-model/{cfg.embed_model}"
            ],
        ))

        rule = events.Rule(
            self, "DailyIngest",
            schedule=events.Schedule.rate(Duration.days(1)),
        )
        # DLQ catches EventBridge failing to *launch* the task (e.g. RunTask throttled
        # or the task fails to start). Runtime crashes are caught separately below.
        launch_dlq = sqs.Queue(
            self, "IngestLaunchDlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        rule.add_target(targets.EcsTask(
            cluster=cluster,
            task_definition=task_def,
            task_count=1,
            subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            assign_public_ip=True,
            retry_attempts=2,
            max_event_age=Duration.hours(2),
            dead_letter_queue=launch_dlq,
        ))

        # --- Monitoring / alerting ---
        alerts = sns.Topic(self, "IngestAlerts", display_name="pep-oracle ingest alerts")
        alerts.add_subscription(subs.EmailSubscription(cfg.alert_email or cfg.allowed_email))

        # 1) Ingest task crashed: an ECS task in this cluster STOPPED with a non-zero
        #    container exit code. (Launch failures are covered by the DLQ alarm below.)
        failure_rule = events.Rule(
            self, "IngestTaskFailed",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [cluster.cluster_arn],
                    "lastStatus": ["STOPPED"],
                    "containers": {"exitCode": [{"anything-but": 0}]},
                },
            ),
        )
        failure_rule.add_target(targets.SnsTopic(
            alerts,
            message=events.RuleTargetInput.from_text(
                "pep-oracle daily ingest task FAILED with a non-zero exit code. "
                "Check the ECS task logs (log group prefix 'ingest')."
            ),
        ))

        # 2) Stale corpus: a daily Lambda publishes the corpus age; alarm if too old.
        stale_check = lambda_.Function(
            self, "CorpusStaleCheck",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_STALE_CHECK_CODE),
            timeout=Duration.seconds(30),
            environment={
                "CORPUS_BUCKET": cfg.corpus_bucket_name,
                "METRIC_NAMESPACE": METRIC_NAMESPACE,
            },
        )
        corpus_bucket.grant_read(stale_check)
        data_key.grant_decrypt(stale_check)
        stale_check.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},
        ))
        events.Rule(
            self, "CorpusStaleCheckSchedule",
            schedule=events.Schedule.rate(Duration.days(1)),
            targets=[targets.LambdaFunction(stale_check)],
        )
        stale_alarm = cloudwatch.Alarm(
            self, "CorpusStaleAlarm",
            metric=cloudwatch.Metric(
                namespace=METRIC_NAMESPACE,
                metric_name="CorpusAgeHours",
                period=Duration.hours(6),
                statistic="Maximum",
            ),
            threshold=CORPUS_STALE_THRESHOLD_HOURS,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.MISSING,
            alarm_description=(
                "No new corpus version published in >10 days — the ingest pipeline "
                "appears stalled (publishes ~weekly normally)."
            ),
        )
        stale_alarm.add_alarm_action(cw_actions.SnsAction(alerts))

        # 3) DLQ alarm: EventBridge couldn't launch the daily task.
        dlq_alarm = cloudwatch.Alarm(
            self, "IngestLaunchDlqAlarm",
            metric=launch_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum",
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="EventBridge failed to launch the daily ingest task (DLQ non-empty).",
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(alerts))

        self.cluster = cluster
        self.task_definition = task_def
        self.alerts_topic = alerts
