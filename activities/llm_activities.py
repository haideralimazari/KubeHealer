import json
import os
import re
from typing import Any

from groq import Groq
from temporalio import activity
from temporalio.exceptions import ApplicationError
from models import Diagnosis

SYSTEM_PROMPT = """You are a Kubernetes SRE expert. You receive pod diagnostic info and must identify the root cause and suggest a fix.
Respond ONLY with valid JSON, no markdown, no explanation outside the JSON:
{
  "pod_name": "the pod name from the input",
  "root_cause": "brief root cause",
  "severity": "low or medium or high",
  "action": "one of: restart_pod, fix_image, patch_resources, skip",
  "explanation": "one sentence a human would understand",
  "fix_details": {}
}
Rules for fix_details:
- If action is fix_image: include {"image": "corrected-image:tag"}
- If action is patch_resources: include {"memory": "128Mi"} or appropriate limit
- If action is restart_pod or skip: empty {}
Common patterns:
- latestt is a typo for latest
- OOMKilled means memory limit is too low, suggest 128Mi or 256Mi
- Missing ConfigMap cannot be auto-fixed, use action skip
"""

VALID_ACTIONS = {"restart_pod", "fix_image", "patch_resources", "skip"}
VALID_SEVERITIES = {"low", "medium", "high"}
IMAGE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]+$")
MEMORY_PATTERN = re.compile(r"^\d+[EPTGMK]i?$")


def _extract_pod_name_from_details(details: str) -> str:
    for line in details.splitlines():
        if line.strip().lower().startswith("pod:"):
            parts = line.split(":", 1)
            if len(parts) > 1:
                return parts[1].strip()
    return ""


def _sanitize_json_text(text: str) -> str:
    cleaned = re.sub(r"\`\`\`(?:json|JSON)?\s*", "", text)
    cleaned = re.sub(r"\`\`\`\s*$", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def _safe_json_load(text: str) -> dict:
    cleaned = _sanitize_json_text(text)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _normalize_action(action: Any) -> str:
    if not action or not isinstance(action, str):
        return "skip"
    normalized = action.strip().lower()
    if normalized not in VALID_ACTIONS:
        return "skip"
    return normalized


def _normalize_severity(severity: Any) -> str:
    if not severity or not isinstance(severity, str):
        return "medium"
    normalized = severity.strip().lower()
    if normalized not in VALID_SEVERITIES:
        return "medium"
    return normalized


def _safe_fix_details(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _validate_diagnosis(data: dict, fallback_pod_name: str) -> dict:
    pod_name = data.get("pod_name") or fallback_pod_name or "unknown"
    if fallback_pod_name and pod_name != fallback_pod_name:
        activity.logger.warning(
            f"LLM pod_name '{pod_name}' differs from expected pod '{fallback_pod_name}'. Using expected pod name."
        )
        pod_name = fallback_pod_name

    action = _normalize_action(data.get("action"))
    severity = _normalize_severity(data.get("severity"))
    root_cause = (data.get("root_cause") or "Unable to identify root cause").strip()
    explanation = (data.get("explanation") or "No explanation provided").strip()
    fix_details = _safe_fix_details(data.get("fix_details"))

    if action == "fix_image":
        image = fix_details.get("image", "")
        if not image or not IMAGE_PATTERN.match(image):
            activity.logger.warning(
                f"Invalid fix_image details: {fix_details}. Falling back to skip."
            )
            return {
                "pod_name": pod_name,
                "root_cause": root_cause,
                "severity": severity,
                "action": "skip",
                "explanation": (
                    "LLM suggested fix_image, but the image value was missing or invalid. "
                    "Review manually."
                ),
                "fix_details": {},
            }

    if action == "patch_resources":
        memory = fix_details.get("memory", "")
        if not memory or not MEMORY_PATTERN.match(memory):
            activity.logger.warning(
                f"Invalid patch_resources details: {fix_details}. Falling back to skip."
            )
            return {
                "pod_name": pod_name,
                "root_cause": root_cause,
                "severity": severity,
                "action": "skip",
                "explanation": (
                    "LLM suggested patch_resources, but the memory value was missing or invalid. "
                    "Review manually."
                ),
                "fix_details": {},
            }

    return {
        "pod_name": pod_name,
        "root_cause": root_cause,
        "severity": severity,
        "action": action,
        "explanation": explanation,
        "fix_details": fix_details,
    }


@activity.defn
async def diagnose_pod(pod_details: str) -> Diagnosis:
    activity.logger.info("Asking Groq to diagnose pod")

    ai = Groq(api_key=os.environ.get("GROQ_API_KEY"))

    try:
        response = ai.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1024,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": pod_details},
            ],
            response_format={"type": "json_object"},
        )
    except Exception as e:
        raise ApplicationError(f"Groq API error: {e}", non_retryable=True)

    raw_text = response.choices[0].message.content or ""
    fallback_pod_name = _extract_pod_name_from_details(pod_details)

    if not raw_text.strip():
        activity.logger.warning("Empty diagnosis response, defaulting to skip")
        return Diagnosis(
            pod_name=fallback_pod_name or "unknown",
            root_cause="No diagnosis returned by LLM",
            severity="high",
            action="skip",
            explanation="LLM returned an empty diagnosis response.",
            fix_details={},
        )

    try:
        data = _safe_json_load(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        activity.logger.error(
            f"Failed to parse Groq diagnosis JSON: {e}. Raw: {raw_text[:200]}"
        )
        return Diagnosis(
            pod_name=fallback_pod_name or "unknown",
            root_cause="Could not parse LLM diagnosis",
            severity="high",
            action="skip",
            explanation=(
                "Unable to parse the LLM response into a valid diagnosis. "
                "Manual review is required."
            ),
            fix_details={},
        )

    validated = _validate_diagnosis(data, fallback_pod_name)
    diagnosis = Diagnosis(
        pod_name=validated["pod_name"],
        root_cause=validated["root_cause"],
        severity=validated["severity"],
        action=validated["action"],
        explanation=validated["explanation"],
        fix_details=validated["fix_details"],
    )
    activity.logger.info(f"Diagnosis: [{diagnosis.severity.upper()}] {diagnosis.root_cause}")
    activity.logger.info(f"Action: {diagnosis.action} — {diagnosis.explanation}")
    return diagnosis
