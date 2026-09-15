from __future__ import annotations

import os
from pathlib import Path


ALLOWED_LOCAL_ENVIRONMENT_KEYS = {"OPENROUTER_API_KEY", "FIRMSIGHT_AI_PROVIDER", "FIRMSIGHT_AI_API_KEY", "FIRMSIGHT_9ROUTER_API_KEY", "FIRMSIGHT_AI_ENDPOINT", "FIRMSIGHT_AI_USER_AGENT", "FIRMSIGHT_AI_MAX_TOKENS", "FIRMSIGHT_AI_STRUCTURED_OUTPUT_MODE", "FIRMSIGHT_AI_REASONING_EFFORT", "FIRMSIGHT_MODEL_INVESTIGATOR", "FIRMSIGHT_MODEL_VERIFIER", "FIRMSIGHT_MODEL_CHAT", "FIRMSIGHT_MODEL_YAML_GENERATOR", "FIRMSIGHT_MODEL_MEMORY_SYNTHESIZER", "FIRMSIGHT_MODEL_MEMORY_VERIFIER", "FIRMSIGHT_IMPORT_ROOT"}


def load_local_environment(path: Path) -> None:
    """Load local secret configuration without overriding deployment environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in ALLOWED_LOCAL_ENVIRONMENT_KEYS and key not in os.environ:
            os.environ[key] = value
