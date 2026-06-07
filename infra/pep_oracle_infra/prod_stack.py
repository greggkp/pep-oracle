"""ap-southeast-2 prod stack: data layer, Cognito, Lambda + Function URL, CloudFront,
Route 53 alias. Resources are added in Tasks 4-7.
"""

from __future__ import annotations

from typing import Optional

from aws_cdk import Stack
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
        # Task 5: Cognito user pool + domain + app client
        # Task 6: Lambda (container) + Function URL + IAM
        # Task 7: CloudFront (cert cross-region) + Route 53 alias
