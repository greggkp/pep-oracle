"""Typed deploy-time config for the pep-oracle CDK app.

Values come from cdk.json context (overridable with -c key=value). One source of
truth shared by app.py and the stacks. Mirrors the runtime env-var contract in
src/pep_oracle/config.py — the Lambda env is set from these in prod_stack.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeployConfig:
    domain_name: str
    compute_region: str
    cert_region: str
    corpus_bucket_name: str
    cognito_domain_prefix: str
    allowed_email: str
    # runtime contract (matches src/pep_oracle/config.py defaults)
    embed_model: str = "amazon.titan-embed-text-v2:0"
    embed_dims: str = "1024"
    oauth_table_name: str = "pep-oracle-oauth"
    signing_ssm_param: str = "/pep-oracle/oauth-signing-key"

    @property
    def public_url(self) -> str:
        return f"https://{self.domain_name}"

    @classmethod
    def from_node(cls, node) -> "DeployConfig":
        def ctx(key: str, default=None):
            val = node.try_get_context(key)
            return val if val is not None else default

        return cls(
            domain_name=ctx("domain_name", "pep-oracle.iicapn.com"),
            compute_region=ctx("compute_region", "ap-southeast-2"),
            cert_region=ctx("cert_region", "us-east-1"),
            corpus_bucket_name=ctx("corpus_bucket_name", "pep-oracle-corpus-prod"),
            cognito_domain_prefix=ctx("cognito_domain_prefix", "pep-oracle-prod"),
            allowed_email=ctx("allowed_email", "REPLACE_ME@example.com"),
        )
