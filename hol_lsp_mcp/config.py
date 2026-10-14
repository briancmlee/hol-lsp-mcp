"""Configuration from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _default_hol_bin() -> str:
    holdir = os.environ.get("HOLDIR")
    if holdir:
        return os.path.join(holdir, "bin", "hol")
    return os.path.expanduser("~/HOL/bin/hol")


# HOL's own default (1500ms) is too short for slow tactics such as CF's
# xmatch: every goalState after one reports "walker timed out".
DEFAULT_GOALSTATE_BUDGET_MS = "10000"


@dataclass
class Config:
    hol_bin: str
    extra_path: str | None
    max_servers: int
    init_timeout: float = 300.0   # loading a big heap can be slow
    request_timeout: float = 120.0
    quiet_period: float = 1.0     # proof pool settle detection
    # HOL_LSP_GOALSTATE_BUDGET_MS for the servers: the goalState walker's
    # time budget in ms.  None leaves the server's environment alone.
    goalstate_budget_ms: str | None = DEFAULT_GOALSTATE_BUDGET_MS

    @classmethod
    def from_env(cls) -> "Config":
        try:
            max_servers = max(1, int(os.environ.get("HOL_LSP_MCP_MAX_SERVERS", "3")))
        except ValueError:
            max_servers = 3
        extra = os.environ.get("HOL_LSP_EXTRA_PATH") or None
        if extra:
            extra = os.pathsep.join(os.path.expanduser(p) for p in extra.split(os.pathsep))
        return cls(
            hol_bin=os.path.expanduser(os.environ.get("HOL_BIN") or _default_hol_bin()),
            extra_path=extra,
            max_servers=max_servers,
            goalstate_budget_ms=(os.environ.get("HOL_LSP_GOALSTATE_BUDGET_MS")
                                 or DEFAULT_GOALSTATE_BUDGET_MS),
        )
