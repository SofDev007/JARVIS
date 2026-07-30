"""Browser actions: the web behind the same gates as everything else.

Risk taxonomy, and the reasoning:

* ``browser_open`` / ``browser_extract_text`` — SENSITIVE. Navigation
  reveals intent and pages can be sensitive, but nothing is changed in
  the world. Scheme is restricted to http/https and, when
  ``browser.allowed_domains`` is configured, the host must match it
  (exact or subdomain).
* ``browser_fill`` / ``browser_click`` — DANGEROUS. Clicking and typing
  are how purchases happen, forms submit and emails send; the M3 clamp
  guarantees a human approves each one regardless of configuration.
* ``browser_fill_secret`` — DANGEROUS, and the login-flow primitive
  without insecure password storage: parameters carry a **secret name**;
  the value is resolved from the secret store inside the handler, typed
  into the page, and never appears in params, results, the audit log or
  the LLM prompt. Two hard rules on top of the gate: it refuses to run
  at all unless ``browser.allowed_domains`` is non-empty, and refuses
  unless the *current page's host* is on that list — a confirmation
  dialog should never be the only thing between a secret and a phishing
  page.
* ``browser_close`` — SAFE. Releasing the browser harms nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from urllib.parse import urlsplit

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.browser.driver import DriverHolder
from digital_twin.configuration.settings import BrowserConfig
from digital_twin.security.permissions import RiskLevel
from digital_twin.security.secrets import SecretStore, check_secret_name

logger = logging.getLogger(__name__)

_MAX_SELECTOR_LEN = 300
_MAX_FILL_LEN = 5000


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for domain in allowed:
        domain = domain.lower().rstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def check_url(raw: Any, allowed_domains: tuple[str, ...]) -> str:
    """Validate a navigation target (raises ``ValueError``)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("a non-empty 'url' string is required")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"only http/https URLs are allowed (got {raw!r})")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"URL has no host: {raw!r}")
    if allowed_domains and not _host_allowed(host, allowed_domains):
        raise ValueError(
            f"host '{host}' is not in browser.allowed_domains "
            f"{sorted(allowed_domains)}"
        )
    return raw


def _require_allowed_host(holder: DriverHolder,
                          allowed: tuple[str, ...],
                          action: str) -> None:
    """When a domain allow-list is configured, interaction actions must
    be on an allow-listed page *at execution time* — validation-time URL
    checks cannot see redirects (M14 hardening; previously only
    ``browser_fill_secret`` re-checked)."""
    if not allowed:
        return
    current = holder.driver().current_url() if holder.active else ""
    host = (urlsplit(current).hostname or "").lower()
    if not host or not _host_allowed(host, allowed):
        raise ValueError(
            f"current page host {host or '(none)'!r} is not in "
            f"browser.allowed_domains — refusing {action}"
        )


def _check_selector(params: Mapping[str, Any]) -> str:
    selector = params.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError("a non-empty 'selector' string is required")
    if len(selector) > _MAX_SELECTOR_LEN:
        raise ValueError(f"'selector' exceeds {_MAX_SELECTOR_LEN} characters")
    return selector


def register_browser_actions(
    registry: ActionRegistry,
    holder: DriverHolder,
    secrets: SecretStore,
    config: BrowserConfig,
) -> None:
    """Register browser actions onto ``registry``."""
    allowed = tuple(config.allowed_domains)
    timeout = config.navigation_timeout_s

    # -- browser_open (SENSITIVE) ------------------------------------------
    def validate_open(params: Mapping[str, Any]) -> None:
        check_url(params.get("url"), allowed)

    def handle_open(params: Mapping[str, Any]) -> str:
        url = check_url(params.get("url"), allowed)
        title = holder.driver().open(url, timeout)
        return f"opened {url}" + (f" — {title[:80]}" if title else "")

    registry.register(ActionSpec(
        name="browser_open",
        description="Navigate the browser to an http(s) URL.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_open,
        validate=validate_open,
    ))

    # -- browser_extract_text (SENSITIVE) ------------------------------------
    def validate_extract(params: Mapping[str, Any]) -> None:
        max_chars = params.get("max_chars")
        if max_chars is not None and (
            not isinstance(max_chars, int) or max_chars < 1
        ):
            raise ValueError("'max_chars' must be a positive integer")

    def handle_extract(params: Mapping[str, Any]) -> str:
        if not holder.active:
            raise ValueError("no page is open — use browser_open first")
        requested = params.get("max_chars") or config.max_extract_chars
        bound = min(int(requested), config.max_extract_chars)
        text = holder.driver().extract_text(bound)
        return f"page text ({len(text)} chars): {text}"

    registry.register(ActionSpec(
        name="browser_extract_text",
        description="Extract bounded visible text from the current page.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_extract,
        validate=validate_extract,
    ))

    # -- browser_fill (DANGEROUS) ---------------------------------------------
    def validate_fill(params: Mapping[str, Any]) -> None:
        _check_selector(params)
        text = params.get("text")
        if not isinstance(text, str):
            raise ValueError("a 'text' string is required")
        if len(text) > _MAX_FILL_LEN:
            raise ValueError(f"'text' exceeds {_MAX_FILL_LEN} characters")

    def handle_fill(params: Mapping[str, Any]) -> str:
        selector = _check_selector(params)
        _require_allowed_host(holder, allowed, "browser_fill")
        holder.driver().fill(selector, str(params["text"]), timeout)
        return f"filled {selector}"

    registry.register(ActionSpec(
        name="browser_fill",
        description="Type text into a page element (forms change the world).",
        risk=RiskLevel.DANGEROUS,
        handler=handle_fill,
        validate=validate_fill,
    ))

    # -- browser_click (DANGEROUS) ----------------------------------------------
    def validate_click(params: Mapping[str, Any]) -> None:
        _check_selector(params)

    def handle_click(params: Mapping[str, Any]) -> str:
        selector = _check_selector(params)
        _require_allowed_host(holder, allowed, "browser_click")
        holder.driver().click(selector, timeout)
        return f"clicked {selector}"

    registry.register(ActionSpec(
        name="browser_click",
        description="Click a page element (submits, purchases, sends).",
        risk=RiskLevel.DANGEROUS,
        handler=handle_click,
        validate=validate_click,
    ))

    # -- browser_fill_secret (DANGEROUS; allow-list mandatory) --------------------
    def validate_fill_secret(params: Mapping[str, Any]) -> None:
        _check_selector(params)
        check_secret_name(params.get("secret"))
        if not allowed:
            raise ValueError(
                "browser_fill_secret refuses to run: configure "
                "browser.allowed_domains first — secrets are never typed "
                "into pages that were not explicitly allow-listed"
            )

    def handle_fill_secret(params: Mapping[str, Any]) -> str:
        selector = _check_selector(params)
        name = check_secret_name(params.get("secret"))
        if not allowed:
            raise ValueError("browser.allowed_domains is not configured")
        current = holder.driver().current_url() if holder.active else ""
        host = (urlsplit(current).hostname or "").lower()
        if not host or not _host_allowed(host, allowed):
            raise ValueError(
                f"current page host {host or '(none)'!r} is not in "
                f"browser.allowed_domains — refusing to fill secret '{name}'"
            )
        value = secrets.get(name)  # resolved at the last moment…
        holder.driver().fill(selector, value, timeout)
        del value  # …and immediately forgotten
        return f"filled {selector} from secret '{name}' on {host}"

    registry.register(ActionSpec(
        name="browser_fill_secret",
        description=("Fill a page element with a stored secret BY NAME "
                     "(value never enters params, results or the audit; "
                     "allow-listed hosts only)."),
        risk=RiskLevel.DANGEROUS,
        handler=handle_fill_secret,
        validate=validate_fill_secret,
    ))

    # -- browser_close (SAFE) ---------------------------------------------------
    def handle_close(params: Mapping[str, Any]) -> str:
        if not holder.active:
            return "browser was not open"
        holder.close()
        return "browser closed"

    registry.register(ActionSpec(
        name="browser_close",
        description="Close the browser and release its resources.",
        risk=RiskLevel.SAFE,
        handler=handle_close,
    ))
