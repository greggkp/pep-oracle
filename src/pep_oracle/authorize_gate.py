"""/oauth/authorize identity gate (Phase 2b2).

Two gates behind one ``AuthorizeGate`` protocol, selected by ``config.AUTHORIZE_GATE``:
  - ``TrustedUpstreamGate`` ("trusted_upstream", default): no in-app identity check;
    /oauth/authorize auto-approves, relying on an upstream authenticator (Cloudflare
    Access). The OptiPlex model -- unchanged behavior.
  - ``CognitoGate`` ("cognito"): /oauth/authorize brokers a login through a one-user
    Cognito user pool (Hosted UI). The browser is bounced to Cognito; the callback
    exchanges the code, verifies the ID token (RS256 via the pool JWKS) and the
    caller's email against an allow-list, then issues the pep-oracle auth code.
    Removes the external-edge dependency and the fail-open-if-misconfigured risk.

Code exchange (`_exchange_code`) and ID-token verification (`_verify_id_token`) land
in later steps; this module starts with construction + the login-redirect URL.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

from pep_oracle import config

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/oauth/authorize/callback"
_HTTP_TIMEOUT = 15


class IdentityError(Exception):
    """Cognito login/verification failed. Opaque -- don't leak which check failed."""


class AuthorizeGate(Protocol):
    def requires_identity(self) -> bool: ...


class TrustedUpstreamGate:
    """No in-app identity check (relies on an upstream authenticator)."""

    def requires_identity(self) -> bool:
        return False


class CognitoGate:
    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        user_pool_id: str,
        region: str,
        allowed_emails: list[str],
    ) -> None:
        self.domain = domain.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_pool_id = user_pool_id
        self.region = region
        self.allowed_emails = [e.strip().lower() for e in allowed_emails if e.strip()]
        self._jwks_cache: Optional[dict[str, Any]] = None

    @classmethod
    def from_config(cls) -> "CognitoGate":
        missing = [
            name
            for name, val in (
                ("PEP_ORACLE_COGNITO_DOMAIN", config.COGNITO_DOMAIN),
                ("PEP_ORACLE_COGNITO_CLIENT_ID", config.COGNITO_CLIENT_ID),
                ("PEP_ORACLE_COGNITO_CLIENT_SECRET", config.COGNITO_CLIENT_SECRET),
                ("PEP_ORACLE_COGNITO_USER_POOL_ID", config.COGNITO_USER_POOL_ID),
            )
            if not val
        ]
        emails = [e.strip().lower() for e in config.COGNITO_ALLOWED_EMAILS.split(",") if e.strip()]
        if not emails:
            missing.append("PEP_ORACLE_COGNITO_ALLOWED_EMAILS")
        if missing:
            raise ValueError("AUTHORIZE_GATE=cognito requires: " + ", ".join(missing))
        return cls(
            domain=config.COGNITO_DOMAIN,
            client_id=config.COGNITO_CLIENT_ID,
            client_secret=config.COGNITO_CLIENT_SECRET,
            user_pool_id=config.COGNITO_USER_POOL_ID,
            region=config.COGNITO_REGION,
            allowed_emails=emails,
        )

    def requires_identity(self) -> bool:
        return True

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    def login_redirect(self, *, redirect_uri: str, login_state: str) -> str:
        """Cognito Hosted UI authorize URL to bounce the browser to."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": login_state,
        }
        return f"{self.domain}/oauth2/authorize?{urlencode(params)}"


def get_gate() -> AuthorizeGate:
    if config.AUTHORIZE_GATE == "cognito":
        return CognitoGate.from_config()
    if config.AUTHORIZE_GATE == "trusted_upstream":
        return TrustedUpstreamGate()
    raise ValueError(f"unknown PEP_ORACLE_AUTHORIZE_GATE: {config.AUTHORIZE_GATE!r}")
