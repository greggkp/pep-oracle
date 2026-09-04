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
    # Where ingest monitoring alarms are emailed. Defaults to allowed_email (set in
    # from_node) so a single -c allowed_email=... covers both; override with
    # -c alert_email=... to send alerts somewhere other than the Cognito allow-list.
    alert_email: str = ""
    git_sha: str = "unknown"  # code provenance for GET /version; pass `-c git_sha=...`
    semver: str = "unknown"  # release tag for GET /version; pass `-c semver=...`
    # runtime contract (matches src/pep_oracle/config.py defaults)
    embed_model: str = "amazon.titan-embed-text-v2:0"
    embed_dims: str = "1024"
    oauth_table_name: str = "pep-oracle-oauth"
    signing_ssm_param: str = "/pep-oracle/oauth-signing-key"
    cognito_client_secret_name: str = "pep-oracle/cognito-client-secret"
    cognito_client_secret_cache_seconds: int = 300
    # KMS CMK id for the corpus bucket / data-at-rest. A fresh deployment creates a
    # new key in PepOracleProdStack; record that key's UUID here (or pass
    # `-c data_key_id=...`) before enabling ingestion. There is deliberately no
    # historical default: a syntactically valid ARN for a deleted key lets the ingest
    # stack deploy and then fail only when its task first uses KMS.
    data_key_id: str = ""
    # Historical hibernation switch. `true` keeps the serving and ingest schedules
    # disabled and permits the reference app to synthesize before a replacement data
    # key has been recorded. Full decommissioning superseded flag-only restoration;
    # see docs/aws/hibernation-runbook.md.
    hibernate: bool = False
    # 0 = no reserved concurrency (default). A reservation needs the account's
    # unreserved pool to stay >= 10, so it's unusable on the default-10 account
    # limit; set via `-c lambda_reserved_concurrency=N` once the quota is raised.
    lambda_reserved_concurrency: int = 0

    @property
    def public_url(self) -> str:
        return f"https://{self.domain_name}"

    @classmethod
    def from_node(cls, node) -> DeployConfig:
        def ctx(key: str, default=None):
            val = node.try_get_context(key)
            return val if val is not None else default

        def ctx_bool(key: str, default: bool) -> bool:
            # cdk.json supplies a real JSON bool; `-c key=value` always supplies a string.
            val = node.try_get_context(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in {"1", "true", "yes"}

        return cls(
            domain_name=ctx("domain_name", "pep-oracle.iicapn.com"),
            compute_region=ctx("compute_region", "ap-southeast-2"),
            cert_region=ctx("cert_region", "us-east-1"),
            corpus_bucket_name=ctx("corpus_bucket_name", "pep-oracle-corpus-prod"),
            cognito_domain_prefix=ctx("cognito_domain_prefix", "pep-oracle-prod"),
            allowed_email=ctx("allowed_email", "REPLACE_ME@example.com"),
            alert_email=ctx("alert_email", ctx("allowed_email", "REPLACE_ME@example.com")),
            git_sha=ctx("git_sha", "unknown"),
            semver=ctx("semver", "unknown"),
            hibernate=ctx_bool("hibernate", False),
            lambda_reserved_concurrency=int(ctx("lambda_reserved_concurrency", 0)),
            cognito_client_secret_cache_seconds=int(
                ctx("cognito_client_secret_cache_seconds", 300)
            ),
            data_key_id=ctx("data_key_id", cls.data_key_id),
        )
