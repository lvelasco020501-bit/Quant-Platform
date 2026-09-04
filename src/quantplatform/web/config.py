"""Where Mission Control looks, and what it is allowed to bind to.

Every field decides *where the dashboard reads from* or *who can reach it*. There is
deliberately no field that names an exchange, a credential, an order endpoint or a strategy
parameter: this service has no such concepts to configure, because it has no code that could
use them.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from quantplatform.core.errors import ConfigurationError

__all__ = ["SCHEMA_VERSION", "WebSettings"]

SCHEMA_VERSION = "2.0.0"
"""Stamped on every response. Bumped from Control Center v1, whose read model could not
parse a schema-2 snapshot at all."""

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

_TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")
"""The CGNAT range Tailscale assigns from. An address inside it is reachable only by devices
on the same tailnet, which is the exposure this service is designed for."""


class WebSettings(BaseSettings):
    """Runtime configuration for the read-only dashboard."""

    model_config = SettingsConfigDict(env_prefix="MISSION_CONTROL_", extra="forbid", frozen=True)
    """Prefixed outside the platform's own ``QP_`` namespace, deliberately.

    ``Settings`` reads every ``QP_`` variable and forbids extras, so a dashboard variable
    named ``QP_WEB_HOST`` is not merely ignored by it — it makes the whole platform
    configuration refuse to load, and the page 500s with a message about a field nobody
    was asking it to validate. Control Center v1 used its own prefix for this reason; the
    lesson is cheaper to keep than to relearn.
    """

    host: str = "127.0.0.1"
    """Bind address. Loopback by default: a monitoring page must never end up reachable
    because nobody set anything."""

    port: int = Field(default=8800, ge=1, le=65535)
    allow_public_bind: bool = False
    """The only way to bind somewhere that is neither loopback nor a tailnet address, and
    it exists to be refused in review rather than used."""

    session_id: str | None = None
    """Which session to display. ``None`` follows whichever session holds the lock, which is
    what someone opening the page to check on a run actually means."""

    state_directory: Path = Path("var/state")
    reports_directory: Path = Path("var/reports")
    log_directory: Path = Path("var/logs")

    refresh_seconds: int = Field(default=12, ge=3, le=300)
    """How often the page re-asks. Served to the browser so the interval lives in one place
    rather than being duplicated in JavaScript."""

    smoke_hours: float | None = Field(default=None, gt=0)
    """Length of the run being tracked, when one was declared. Without it the page shows
    elapsed time and nothing else — a progress bar against a target nobody stated would be
    an invented number."""

    @model_validator(mode="after")
    def _check_bind(self) -> Self:
        """Refuse a bind address that would publish this beyond the tailnet.

        ``0.0.0.0`` is refused outright and cannot be opted into: it binds every interface
        the machine has, including the public one, and "I meant only the private interface"
        is not something that address can express. A specific address is always available
        and always says what was meant.

        Raises:
            ConfigurationError: If the address is neither loopback nor tailnet, or if it is
                a wildcard.
        """
        if self.host in {"0.0.0.0", "::", "*"}:  # noqa: S104 - naming it to refuse it
            msg = (
                f"refusing to bind Mission Control to {self.host!r}: a wildcard binds every "
                "interface including the public one. Bind the tailnet address explicitly "
                "(for example the machine's 100.x.y.z) so the intent is stated."
            )
            raise ConfigurationError(msg)
        if self.host in _LOOPBACK or self._is_tailnet(self.host):
            return self
        if not self.allow_public_bind:
            msg = (
                f"refusing to bind Mission Control to {self.host!r}: it is neither loopback "
                "nor a Tailscale address, so this would expose an unauthenticated dashboard "
                "to whatever network that interface reaches."
            )
            raise ConfigurationError(msg)
        return self

    @staticmethod
    def _is_tailnet(host: str) -> bool:
        """Return whether an address belongs to the Tailscale CGNAT range."""
        try:
            return ipaddress.ip_address(host) in _TAILSCALE_RANGE
        except ValueError:
            return False
