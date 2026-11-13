"""Central configuration for the Task Flow Automation project.

Change values here so the whole team stays consistent.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Timezone used by the deterministic datetime resolver (datetime_resolver).
TIMEZONE = "Asia/Kolkata"

# --- Extraction provider --------------------------------------------------------
# "cohere" (default) or "hermes" (OpenAI-compatible endpoint: Nous Portal,
# OpenRouter, vLLM, Ollama...). Switching is a one-variable change.
EXTRACTION_PROVIDER = os.environ.get("EXTRACTION_PROVIDER", "cohere").strip().lower()

# Cohere
COHERE_MODEL = os.environ.get("COHERE_MODEL", "command-a-plus-05-2026")

# Hermes
HERMES_BASE_URL = os.environ.get("HERMES_BASE_URL", "https://api.nousresearch.com/v1")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "nousresearch/hermes-4-70b")
# Hermes 4 is hybrid-reasoning; extraction wants direct mode (faster, cheaper).
HERMES_DISABLE_REASONING = (
    os.environ.get("HERMES_DISABLE_REASONING", "true").strip().lower()
    not in {"0", "false", "no"}
)
