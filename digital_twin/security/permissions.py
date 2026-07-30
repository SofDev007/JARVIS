"""Permission policy: who may do what, decided before anything runs.

Least-privilege by construction:

* Every action carries a :class:`RiskLevel`; the policy maps risk levels to
  a default decision (``allow`` / ``confirm`` / ``deny``) and allows
  per-action overrides from configuration.
* Anything unknown or unconfigured resolves to **deny** — absence of a rule
  is never permission.
* ``DANGEROUS`` actions have a hard floor of **confirm**: configuration can
  tighten their handling to ``deny`` but can never silently ``allow`` them.
  An assistant that can be config-tricked into unattended dangerous actions
  is broken; this invariant is enforced in code, not convention.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """How much damage an action could do if triggered unintentionally."""

    SAFE = "safe"            # logging, notifications — no side effects beyond UX
    SENSITIVE = "sensitive"  # opens apps/URLs, touches user context
    DANGEROUS = "dangerous"  # destructive or hard-to-undo (none built-in yet)


class Decision(Enum):
    """Outcome of a permission evaluation."""

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class PermissionPolicy:
    """Evaluates action permissions from risk defaults + per-action overrides."""

    def __init__(
        self,
        risk_defaults: dict[str, str] | None = None,
        overrides: dict[str, str] | None = None,
    ):
        self._risk_defaults = dict(risk_defaults or {})
        self._overrides = dict(overrides or {})

    def evaluate(self, action_name: str, risk: RiskLevel) -> Decision:
        """Return the decision for one action; unknown rules deny."""
        rule = self._overrides.get(action_name)
        if rule is None:
            rule = self._risk_defaults.get(risk.value, Decision.DENY.value)
        try:
            decision = Decision(rule)
        except ValueError:
            logger.error(
                "Invalid permission rule %r for action %r; denying", rule, action_name
            )
            return Decision.DENY

        if risk is RiskLevel.DANGEROUS and decision is Decision.ALLOW:
            logger.warning(
                "Action %r is DANGEROUS; clamping configured 'allow' to "
                "'confirm' (dangerous actions always require confirmation)",
                action_name,
            )
            return Decision.CONFIRM
        return decision
