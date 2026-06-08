"""ap-southeast-2 ingestion stack (Phase 3): a daily EventBridge rule runs a scale-to-zero
Fargate task that incrementally ingests new episodes and publishes a new corpus version to
S3. Modal does the GPU transcribe/diarize; this task orchestrates + Bedrock-embeds.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from pep_oracle_infra.config import DeployConfig

# SSM SecureString params holding the Modal credentials (created out-of-band; see runbook).
MODAL_TOKEN_ID_PARAM = "/pep-oracle/modal-token-id"
MODAL_TOKEN_SECRET_PARAM = "/pep-oracle/modal-token-secret"


class PepOracleIngestStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: DeployConfig,
        data_key: kms.IKey,
        corpus_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, cid, **kwargs)

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
        rule.add_target(targets.EcsTask(
            cluster=cluster,
            task_definition=task_def,
            task_count=1,
            subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            assign_public_ip=True,
        ))

        self.cluster = cluster
        self.task_definition = task_def
