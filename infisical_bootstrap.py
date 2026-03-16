"""Runtime bootstrap for loading application secrets from Infisical.

This module intentionally only relies on standard library modules so it can be
used very early during app startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

import requests

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED = False


def _logger(provided: Optional[logging.Logger] = None) -> logging.Logger:
    return provided or logging.getLogger(__name__)


def _base_url() -> str:
    raw = (os.getenv("INFISICAL_BASE_URL") or "https://app.infisical.com").strip()
    return raw.rstrip("/")


def _required_env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _parse_secrets(payload: Dict[str, Any]) -> Dict[str, str]:
    output: Dict[str, str] = {}
    for item in payload.get("secrets") or []:
        key = item.get("secretKey") or item.get("key")
        value = item.get("secretValue")
        if value is None:
            value = item.get("secret")
        if key and value is not None:
            output[str(key)] = str(value)
    return output


def bootstrap_infisical_secrets(*, force: bool = False, timeout_seconds: int = 10, logger: Optional[logging.Logger] = None) -> bool:
    """Fetch secrets from Infisical and inject them into os.environ.

    Requires INFISICAL_TOKEN, INFISICAL_PROJECT_ID, and INFISICAL_ENVIRONMENT
    to be already present in environment variables (typically loaded from .env).
    """
    global _BOOTSTRAPPED
    log = _logger(logger)

    token = _required_env("INFISICAL_TOKEN")
    project_id = _required_env("INFISICAL_PROJECT_ID")
    environment = _required_env("INFISICAL_ENVIRONMENT")

    if not token or not project_id or not environment:
        log.warning("Infisical bootstrap skipped: missing INFISICAL_TOKEN / INFISICAL_PROJECT_ID / INFISICAL_ENVIRONMENT")
        return False

    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED and not force:
            return True

        url = f"{_base_url()}/api/v3/secrets/raw"
        params = {
            "workspaceId": project_id,
            "environment": environment,
            "secretPath": "/",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ScanmyData/1.0",
        }

        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            secrets_map = _parse_secrets(payload)
            if not secrets_map:
                log.warning("Infisical bootstrap returned zero secrets")
            for key, value in secrets_map.items():
                os.environ[key] = value
            _BOOTSTRAPPED = True
            log.info("Infisical bootstrap loaded %d secrets", len(secrets_map))
            return True
        except Exception as exc:
            log.warning("Infisical bootstrap failed: %s", exc)
            return False
