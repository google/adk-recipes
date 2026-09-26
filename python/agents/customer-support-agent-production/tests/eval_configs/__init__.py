"""Evaluation config profiles — switch via EVAL_PROFILE env var.

Profiles:
    fast     — Free metrics only (response_match_score). Fast, no LLM judge.
    standard — Balanced. Adds tool_trajectory (unit) or rubric-based LLM judge (integration).
    full     — Comprehensive. Adds final_response_match_v2 LLM-as-judge.

Usage:
    EVAL_PROFILE=fast pytest tests/integration/ -v -s
    EVAL_PROFILE=full pytest tests/unit/ -v -s
    pytest tests/integration/ -v -s              # default = standard

CI/CD mapping:
    PR              → fast       (free, quick feedback)
    Push to main    → standard   (balanced, catches routing issues)
    Nightly         → full       (comprehensive quality check)
    Release gate    → full       (must pass before deploy)
"""

import logging
import os
from pathlib import Path

from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.eval_set import EvalSet

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent
DEFAULT_PROFILE = "standard"
VALID_PROFILES = {"fast", "standard", "standard_exact", "full"}


def get_eval_profile() -> str:
    """Get the active eval profile from EVAL_PROFILE env var."""
    profile = os.environ.get("EVAL_PROFILE", DEFAULT_PROFILE)
    if profile not in VALID_PROFILES:
        logger.warning(
            "[EVAL] Unknown profile '%s', falling back to '%s'. Valid: %s",
            profile,
            DEFAULT_PROFILE,
            ", ".join(sorted(VALID_PROFILES)),
        )
        return DEFAULT_PROFILE
    return profile


def load_eval_config(test_type: str, profile: str = None) -> EvalConfig:
    """Load eval config for the given test type and profile.

    Args:
        test_type: "unit" or "integration"
        profile: Override profile name. Default: from EVAL_PROFILE env var.

    Returns:
        EvalConfig loaded from the profile JSON file.
    """
    profile = profile or get_eval_profile()
    config_path = PROFILES_DIR / test_type / f"{profile}.json"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Eval config not found: {config_path}. " f"Valid profiles: {', '.join(sorted(VALID_PROFILES))}"
        )

    logger.info("[EVAL] Profile=%s Type=%s Config=%s", profile, test_type, config_path)

    with open(config_path) as f:
        return EvalConfig.model_validate_json(f.read())


def load_eval_set(path: str) -> EvalSet:
    """Load an EvalSet from a JSON file."""
    with open(path) as f:
        return EvalSet.model_validate_json(f.read())


def load_post_deploy_config(profile: str = None) -> dict:
    """Load post-deploy eval config (Vertex AI Gen AI Eval Service).

    Args:
        profile: Override profile name. Default: from EVAL_PROFILE env var.

    Returns:
        dict with metrics, thresholds, and pointwise_criteria.
    """
    import json

    profile = profile or get_eval_profile()
    config_path = PROFILES_DIR / "post_deploy" / f"{profile}.json"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Post-deploy eval config not found: {config_path}. " f"Valid profiles: {', '.join(sorted(VALID_PROFILES))}"
        )

    logger.info("[EVAL] Post-deploy Profile=%s Config=%s", profile, config_path)

    with open(config_path) as f:
        return json.load(f)
