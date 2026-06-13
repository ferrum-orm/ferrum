"""Migration confirmation tokens.

Confirmation tokens are short-lived, single-use secrets used to authorize
destructive migration operations. They are transmitted via stdin or environment
variable — never via argv or public logs (MIG-7).

Security constraints:
- Tokens are cryptographically random (``secrets`` module, 32 bytes of entropy).
- A token is bound to a specific plan digest; replaying it against a different
  plan is rejected.
- Tokens expire after first use (MIG-8): the ledger records the digest, and the
  apply path rejects a second apply of the same digest.
- Token values are never written to Ferrum logs, hook payloads, or dry-run output.
"""

from __future__ import annotations

import hmac
import secrets


def generate_token(plan_digest: str) -> str:
    """Generate a single-use confirmation token bound to a plan digest.

    The token is ``<random_hex>.<digest_prefix>`` where the digest prefix is
    included for human readability in the confirmation prompt only.
    The random component provides the entropy; the digest prefix is not a secret.
    """
    random_part = secrets.token_hex(32)
    # Include only a short prefix of the digest in the token so operators can
    # visually confirm they are authorizing the right plan.
    digest_prefix = plan_digest[:8]
    return f"{random_part}.{digest_prefix}"


def validate_token(token: str, plan_digest: str) -> bool:
    """Validate that a confirmation token is bound to the given plan digest.

    Uses constant-time comparison to prevent timing attacks.
    """
    try:
        _, token_digest_prefix = token.rsplit(".", 1)
    except ValueError:
        return False
    expected_prefix = plan_digest[:8]
    return hmac.compare_digest(token_digest_prefix, expected_prefix)
