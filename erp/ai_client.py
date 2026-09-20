"""
DeepSeek client used to:
  1) analyze/classify a single lead (analyze_lead) - returns structured JSON
  2) generate free-form text (chat) - used for the weekly report

If DEEPSEEK_API_KEY isn't configured, falls back to a simple default so the
pipeline never blocks (same philosophy as the other demo scripts in this
project: runs out of the box without a live service).
"""
import json
import logging

import requests

from erp.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(DEEPSEEK_API_KEY)


def chat(prompt: str) -> str:
    """Call DeepSeek chat completion, return free-form text. Fallback: a clear error message."""
    if not is_configured():
        return (
            "[DEEPSEEK_API_KEY is not configured - cannot generate an AI report. "
            "Fill in the key in the .env file to enable this feature.]"
        )
    try:
        response = requests.post(
            f"{DEEPSEEK_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except (requests.exceptions.RequestException, KeyError) as e:
        logger.error(f"DeepSeek call failed: {e}")
        return f"[Error calling DeepSeek: {e}]"


def analyze_lead(lead: dict) -> dict:
    """
    Analyze a lead: scale, potential, organization type, notes for the sales rep.
    Returns a dict with keys: scale_estimate, potential_score, organization_type,
    ai_notes, raw_response.
    """
    if not is_configured():
        logger.warning("DeepSeek is not configured - using a simple heuristic fallback.")
        return _fallback_analysis(lead)

    prompt = (
        "You are a B2B sales lead analysis assistant. Based on the lead information "
        "below, return ONLY a single JSON object with the fields: "
        "scale_estimate (one of: solo, small, medium, enterprise), "
        "potential_score (integer 0-100), "
        "organization_type (inferred from the company name/industry), "
        "ai_notes (a short note for the sales rep about scale, potential, "
        "and how to approach this lead).\n\n"
        f"Lead information: {json.dumps(lead, ensure_ascii=False)}"
    )

    try:
        response = requests.post(
            f"{DEEPSEEK_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=20,
        )
        response.raise_for_status()
        raw = response.json()
        parsed = json.loads(raw["choices"][0]["message"]["content"])
        parsed["raw_response"] = raw
        return parsed
    except (requests.exceptions.RequestException, KeyError, json.JSONDecodeError) as e:
        logger.error(f"DeepSeek lead analysis call failed: {e}. Using fallback.")
        return _fallback_analysis(lead)


def _fallback_analysis(lead: dict) -> dict:
    company = (lead.get("company") or "").strip()
    scale = "small" if company else "solo"
    return {
        "scale_estimate": scale,
        "potential_score": 50,
        "organization_type": "unknown",
        "ai_notes": (
            "No real AI result yet (DEEPSEEK_API_KEY is not configured). "
            "This is a default fallback note - the sales rep should assess further."
        ),
        "raw_response": None,
    }
