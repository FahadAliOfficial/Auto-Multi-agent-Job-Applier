from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
import urllib.error
import yaml
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.logger import setup_logger


MASTER_RESUME_PATH = Path("config/resume_master.json")
OUTPUT_DIR = Path("data/resume_outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_SCHEMA_VERSION = "v2"
RESUME_MAKER_MODEL = "gpt-4o-mini-2024-07-18"
GEMINI_RESUME_MAKER_MODEL = "gemma-4-31b-it"
LOW_COST_MODE = True
JD_CHAR_LIMIT = 1800
DEFAULT_MAX_OUTPUT_TOKENS = 220
DEFAULT_USE_OPENAI_POLISH = True
OLLAMA_SUMMARY_ENABLED = True
OLLAMA_MODEL = "orca-mini:3b-q4_0"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_JD_CHAR_LIMIT = 5000
logger = setup_logger("resume_maker")


@dataclass
class TailorResult:
    tailored_resume: dict[str, Any]
    changes: list[str]
    model: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    cache_key: str
    cached: bool


def load_master_resume() -> dict[str, Any]:
    if MASTER_RESUME_PATH.exists():
        return json.loads(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    sample = _default_master_resume()
    MASTER_RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    MASTER_RESUME_PATH.write_text(json.dumps(sample, indent=2), encoding="utf-8")
    return sample


def save_master_resume(resume_data: dict[str, Any]) -> None:
    MASTER_RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    MASTER_RESUME_PATH.write_text(json.dumps(resume_data, indent=2), encoding="utf-8")


def tailor_resume(
    master_resume: dict[str, Any],
    job_description: str,
    model: str = RESUME_MAKER_MODEL,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    use_openai_polish: bool = DEFAULT_USE_OPENAI_POLISH,
    ai_provider: str = "openai",
) -> TailorResult:
    jd = (job_description or "").strip()
    base = deepcopy(master_resume)
    if not jd:
        return TailorResult(
            tailored_resume=base,
            changes=["No job description provided; returned master resume unchanged."],
            model=model,
            estimated_input_tokens=0,
            estimated_output_tokens=0,
            cache_key="",
            cached=False,
        )

    provider = (ai_provider or "openai").strip().lower()
    if provider not in {"openai", "gemini"}:
        provider = "openai"
    api_key_available = bool(_load_openai_api_key() if provider == "openai" else _load_gemini_api_key())
    local_summary_enabled = _resume_maker_use_local_summary()
    cache_key = _cache_key(
        base,
        jd,
        model,
        api_key_available=api_key_available,
        use_openai_polish=use_openai_polish,
        ai_provider=provider,
        local_summary_enabled=local_summary_enabled,
    )
    cache_file = OUTPUT_DIR / f"{cache_key}.json"
    if cache_file.exists():
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        changes = payload.get("changes", [])
        stale_no_key = (
            api_key_available
            and isinstance(changes, list)
            and any(
                "OPENAI_API_KEY not set" in str(c)
                or "GEMINI_API_KEY (or GOOGLE_API_KEY) not set" in str(c)
                for c in changes
            )
        )
        stale_ai_failure = (
            isinstance(changes, list)
            and any("AI refinement failed; used heuristic-only tailoring." in str(c) for c in changes)
        )
        if stale_no_key or stale_ai_failure:
            try:
                cache_file.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            cached_resume = _normalize_tailored_resume(base, payload["tailored_resume"])
            return TailorResult(
                tailored_resume=cached_resume,
                changes=payload["changes"],
                model=payload["model"],
                estimated_input_tokens=payload["estimated_input_tokens"],
                estimated_output_tokens=payload["estimated_output_tokens"],
                cache_key=cache_key,
                cached=True,
            )

    jd_for_local = jd[:OLLAMA_JD_CHAR_LIMIT]
    jd_summary = _summarize_jd_with_ollama(jd_for_local) if local_summary_enabled else ""
    jd_compact = _build_compact_jd(jd, jd_summary)

    keywords = extract_keywords(jd_compact)
    heuristic = _apply_heuristics(base, keywords)
    ai_call_succeeded = False
    if use_openai_polish:
        ai = _ai_refine(
            heuristic,
            jd_compact,
            model=model,
            max_output_tokens=max_output_tokens,
            ai_provider=provider,
        )
        ai_call_succeeded = bool(ai.get("ai_call_succeeded", False))
    else:
        local_only = _apply_role_focus(_normalize_tailored_resume(base, heuristic), jd_compact)
        ai = {
            "resume": local_only,
            "changes": ["Applied local-only tailoring (no remote AI API call)."],
            "ai_call_succeeded": False,
        }

    result = TailorResult(
        tailored_resume=ai["resume"],
        changes=ai["changes"],
        model=model,
        estimated_input_tokens=estimate_tokens(json.dumps(base) + jd_compact) if use_openai_polish else 0,
        estimated_output_tokens=estimate_tokens(json.dumps(ai["resume"])) if use_openai_polish else 0,
        cache_key=cache_key,
        cached=False,
    )
    should_cache = (not use_openai_polish) or ai_call_succeeded
    if should_cache:
        cache_file.write_text(
            json.dumps(
                {
                    "tailored_resume": result.tailored_resume,
                    "changes": result.changes,
                    "model": result.model,
                    "estimated_input_tokens": result.estimated_input_tokens,
                    "estimated_output_tokens": result.estimated_output_tokens,
                    "created_at": datetime.utcnow().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return result


def _flatten_skills(skills: Any) -> list[str]:
    if isinstance(skills, dict):
        out: list[str] = []
        for items in skills.values():
            if isinstance(items, list):
                out.extend([str(x) for x in items if str(x).strip()])
            elif items:
                out.append(str(items))
        return out
    if isinstance(skills, list):
        return [str(x) for x in skills if str(x).strip()]
    return []


def _get_grouped_skills(resume_data: dict[str, Any]) -> dict[str, list[str]]:
    grouped = resume_data.get("skills_grouped") or resume_data.get("skills_by_category")
    if isinstance(grouped, dict):
        return {
            str(k): [str(x) for x in v] if isinstance(v, list) else [str(v)]
            for k, v in grouped.items()
            if k and v
        }
    skills = resume_data.get("skills")
    if isinstance(skills, dict):
        return {
            str(k): [str(x) for x in v] if isinstance(v, list) else [str(v)]
            for k, v in skills.items()
            if k and v
        }
    return {}


def render_resume_markdown(resume_data: dict[str, Any]) -> str:
    p = resume_data.get("profile", {})
    lines: list[str] = []
    lines.append(f"# {p.get('name', '')}".strip())
    contact_parts = [p.get("email", ""), p.get("phone", ""), p.get("location", ""), p.get("website", "")]
    contact = " | ".join([x for x in contact_parts if x])
    if contact:
        lines.append(contact)
    summary = p.get("summary", "")
    if summary:
        lines.append("")
        lines.append("## Summary")
        lines.append(summary)
    exp = resume_data.get("experience", [])
    if exp:
        lines.append("")
        lines.append("## Experience")
        for job in exp:
            title = job.get("title", "")
            company = job.get("company", "")
            period = job.get("period", "")
            lines.append(f"### {title} — {company}".strip(" —"))
            if period:
                lines.append(period)
            for b in job.get("bullets", []):
                lines.append(f"- {b}")

    projects = resume_data.get("projects", [])
    if projects:
        lines.append("")
        lines.append("## Projects")
        for pr in projects:
            lines.append(f"### {pr.get('name', '')}")
            desc = pr.get("description", "")
            if desc:
                lines.append(desc)
            for b in pr.get("bullets", []):
                lines.append(f"- {b}")

    grouped_skills = _get_grouped_skills(resume_data)
    flat_skills = _flatten_skills(resume_data.get("skills", []))
    if grouped_skills or flat_skills:
        lines.append("")
        lines.append("## Technical Skills")
        if grouped_skills:
            for category, items in grouped_skills.items():
                items_text = ", ".join([x for x in items if x])
                if items_text:
                    lines.append(f"- {category}: {items_text}")
        else:
            lines.append(", ".join(flat_skills))

    edu = resume_data.get("education", [])
    if edu:
        lines.append("")
        lines.append("## Education")
        for e in edu:
            lines.append(f"- {e.get('degree', '')}, {e.get('institution', '')} ({e.get('year', '')})".strip())

    certs = resume_data.get("certifications", [])
    if certs:
        lines.append("")
        lines.append("## Certifications")
        for c in certs:
            lines.append(f"- {c}")

    additional = resume_data.get("additional_info", [])
    if additional:
        lines.append("")
        lines.append("## Additional Information")
        for item in additional:
            lines.append(f"- {item}")

    return "\n".join(lines).strip() + "\n"


def render_resume_html(resume_data: dict[str, Any]) -> str:
    """Public HTML renderer for live preview/export."""
    return _render_resume_html(resume_data)


def export_tailored_resume_files(
    tailored_resume: dict[str, Any],
    job_title_hint: str = "",
) -> dict[str, str]:
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", (job_title_hint or "job")).strip("_").lower()[:60] or "job"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_DIR / f"{stamp}_{safe}"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "resume.json"
    md_path = out_dir / "resume.md"
    html_path = out_dir / "resume.html"

    json_path.write_text(json.dumps(tailored_resume, indent=2), encoding="utf-8")
    md_path.write_text(render_resume_markdown(tailored_resume), encoding="utf-8")
    html_path.write_text(_render_resume_html(tailored_resume), encoding="utf-8")

    return {
        "folder": str(out_dir),
        "json": str(json_path),
        "markdown": str(md_path),
        "html": str(html_path),
    }


def estimate_tokens(text: str) -> int:
    # Fast rough estimate: ~4 chars/token English average
    return max(1, len(text) // 4)


def extract_keywords(job_description: str, limit: int = 24) -> list[str]:
    text = (job_description or "").lower()
    text = re.sub(r"[^a-z0-9+#.\s]", " ", text)
    words = [w for w in text.split() if len(w) > 2]
    stop = {
        "the", "and", "for", "with", "you", "your", "are", "will", "this", "that", "from", "have", "our",
        "not", "but", "all", "can", "has", "who", "job", "role", "team", "work", "years", "year", "using",
    }
    freq: dict[str, int] = {}
    for w in words:
        if w in stop:
            continue
        freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:limit]]


def _apply_heuristics(master_resume: dict[str, Any], keywords: list[str]) -> dict[str, Any]:
    resume = deepcopy(master_resume)
    kw = set(keywords)

    def score_text(text: str) -> int:
        t = (text or "").lower()
        return sum(1 for k in kw if k in t)

    # Reorder bullets by relevance in each job
    for job in resume.get("experience", []):
        bullets = job.get("bullets", [])
        bullets.sort(key=score_text, reverse=True)

    # Reorder skills by relevance
    skills = resume.get("skills", [])
    if isinstance(skills, list):
        skills.sort(key=score_text, reverse=True)
    elif isinstance(skills, dict):
        for key, items in skills.items():
            if isinstance(items, list):
                skills[key] = sorted(items, key=lambda s: score_text(str(s)), reverse=True)
    return resume


def _ai_refine(
    resume_data: dict[str, Any],
    jd: str,
    model: str,
    max_output_tokens: int,
    ai_provider: str = "openai",
) -> dict[str, Any]:
    provider = (ai_provider or "openai").strip().lower()
    if provider == "gemini":
        api_key = _load_gemini_api_key()
        if not api_key:
            return {
                "resume": resume_data,
                "changes": ["GEMINI_API_KEY (or GOOGLE_API_KEY) not set; used heuristic-only tailoring."],
                "ai_call_succeeded": False,
            }
    else:
        provider = "openai"
        api_key = _load_openai_api_key()
        if not api_key:
            return {
                "resume": resume_data,
                "changes": ["OPENAI_API_KEY not set; used heuristic-only tailoring."],
                "ai_call_succeeded": False,
            }

    system = (
        "You are a precise resume editor. Never invent facts. "
        "Return strict JSON only."
    )
    user = {
        "instructions": (
            "Given resume and job description, return ONLY:\n"
            "1) summary: 2-3 concise lines tailored to role.\n"
            "2) changes: up to 3 short bullets.\n"
            "Do not return full resume. Do not use markdown."
        ),
        "job_description": jd[:JD_CHAR_LIMIT],
        "profile": resume_data.get("profile", {}),
        "skills": _flatten_skills(resume_data.get("skills", []))[:30],
        "experience_bullets_sample": [
            b
            for job in (resume_data.get("experience", [])[:2] or [])
            for b in (job.get("bullets", [])[:3] or [])
        ][:6],
    }
    try:
        if provider == "gemini":
            parsed = _gemini_json_refine(
                api_key=api_key,
                model=model,
                system=system,
                user_payload=user,
                max_output_tokens=max_output_tokens,
            )
        else:
            parsed = _chat_json_refine(
                api_key=api_key,
                model=model,
                system=system,
                user_payload=user,
                max_output_tokens=max_output_tokens,
            )
        tailored = deepcopy(resume_data)
        summary = str(parsed.get("summary", "")).strip()
        if summary and isinstance(tailored.get("profile"), dict):
            tailored["profile"]["summary"] = summary
        tailored = _normalize_tailored_resume(resume_data, tailored)
        tailored = _apply_role_focus(tailored, jd)
        claimed_changes = parsed.get("changes") or ["Applied low-cost AI summary refinement."]
        if not isinstance(claimed_changes, list):
            claimed_changes = [str(claimed_changes)]
        changes = _compute_actual_changes(resume_data, tailored, claimed_changes)
        return {"resume": tailored, "changes": changes[:20], "ai_call_succeeded": True}
    except Exception as exc:
        err_msg = f"{exc}"
        logger.error("AI refine failed provider=%s model=%s error=%s", provider, model, err_msg[:300])
        fallback = _apply_role_focus(_normalize_tailored_resume(resume_data, resume_data), jd)
        changes = _compute_actual_changes(
            resume_data,
            fallback,
            [f"{provider.upper()} skipped in low-cost mode due to error: {err_msg[:180]}"],
        )
        return {"resume": fallback, "changes": changes[:20], "ai_call_succeeded": False}


def _gemini_json_refine(
    api_key: str,
    model: str,
    system: str,
    user_payload: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "changes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "changes"],
    }
    prompt = (
        "Return ONLY a JSON object that matches this schema: "
        "{summary: string, changes: string[]}\n"
        "No markdown, no extra text.\n\n"
        f"SYSTEM_RULES:\n{system}\n\n"
        "INPUT_JSON:\n"
        f"{json.dumps(user_payload, ensure_ascii=True)}\n"
    )

    def call_gemini(text_prompt: str) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": text_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (
            (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])[0].get("text", "")
        )
    try:
        text = call_gemini(prompt)
        try:
            return _parse_json_object(str(text))
        except Exception as parse_exc:
            snippet = str(text).strip().replace("\n", " ")[:800]
            logger.error("Gemini raw output (truncated)=%s", snippet)
            # Retry once with a shorter prompt to reduce echoing.
            retry_prompt = (
                "Return ONLY JSON with keys summary and changes. "
                "No extra text.\n\n"
                f"INPUT_JSON:\n{json.dumps(user_payload, ensure_ascii=True)}\n"
            )
            retry_text = call_gemini(retry_prompt)
            try:
                return _parse_json_object(str(retry_text))
            except Exception:
                try:
                    parsed_text = _parse_text_refine_response(str(retry_text))
                    return {"summary": parsed_text.get("summary", ""), "changes": parsed_text.get("changes", [])}
                except Exception:
                    raise parse_exc
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"HTTP {e.code}"
        if body:
            detail += f": {body[:700]}"
        logger.error("Gemini HTTP error model=%s detail=%s", model, detail[:300])
        raise RuntimeError(detail) from e
    except Exception as e:
        logger.error("Gemini request error model=%s error=%s", model, str(e)[:300])
        raise RuntimeError(str(e)) from e


def _responses_call(payload: dict[str, Any], api_key: str, timeout: int = 45) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"HTTP {e.code}"
        if body:
            detail += f": {body[:700]}"
        raise RuntimeError(detail) from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _chat_json_refine(
    api_key: str,
    model: str,
    system: str,
    user_payload: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max_output_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        return _parse_json_object(content)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"HTTP {e.code}"
        if body:
            detail += f": {body[:700]}"
        raise RuntimeError(detail) from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _chat_text_refine(
    api_key: str,
    model: str,
    job_description: str,
    resume_data: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    profile = resume_data.get("profile", {}) if isinstance(resume_data.get("profile", {}), dict) else {}
    current_summary = str(profile.get("summary", "")).strip()
    prompt = (
        "You are a resume editor. Return plain text only in this exact format:\n"
        "SUMMARY:\n"
        "<2-3 lines improved summary tailored to job, factual, no invented claims>\n"
        "CHANGES:\n"
        "- <change 1>\n"
        "- <change 2>\n"
        "- <change 3>\n\n"
        "Do not include JSON, markdown code fences, or any extra sections.\n\n"
        f"Current summary:\n{current_summary}\n\n"
        f"Job description:\n{job_description[:4500]}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Be concise, factual, and ATS-friendly."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_output_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        return _parse_text_refine_response(content)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"HTTP {e.code}"
        if body:
            detail += f": {body[:700]}"
        raise RuntimeError(detail) from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


def _parse_text_refine_response(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise ValueError("Empty text fallback output")
    upper = text.upper()
    s_idx = upper.find("SUMMARY:")
    c_idx = upper.find("CHANGES:")
    summary = ""
    changes: list[str] = []
    if s_idx != -1 and c_idx != -1 and c_idx > s_idx:
        summary = text[s_idx + len("SUMMARY:"):c_idx].strip()
        changes_block = text[c_idx + len("CHANGES:"):].strip()
    else:
        # Heuristic split if headings not perfect.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        summary = lines[0] if lines else ""
        changes_block = "\n".join(lines[1:]) if len(lines) > 1 else ""
    for line in changes_block.splitlines():
        ln = line.strip().lstrip("-").strip()
        if ln:
            changes.append(ln)
    if not changes:
        changes = ["Applied plain-text AI refinement fallback."]
    return {"summary": summary, "changes": changes[:10]}


def _apply_text_fallback_to_resume(base_resume: dict[str, Any], parsed_text: dict[str, Any]) -> dict[str, Any]:
    resume = deepcopy(base_resume)
    summary = str(parsed_text.get("summary", "")).strip()
    if summary and isinstance(resume.get("profile"), dict):
        resume["profile"]["summary"] = summary
    return _normalize_tailored_resume(base_resume, resume)


def _parse_json_object(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("Empty model output")
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Fallback 1: extract first balanced JSON object (escape/string-aware).
    snippet = _extract_first_balanced_json_object(s)
    if snippet:
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # Fallback 2: repair common formatting issues and parse again.
    repaired = _repair_json_text(snippet or s)
    if repaired:
        try:
            obj = json.loads(repaired)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    raise ValueError("Model did not return a valid JSON object")


def _extract_first_balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def _repair_json_text(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    s = (
        s.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _normalize_tailored_resume(base_resume: dict[str, Any], candidate_resume: dict[str, Any]) -> dict[str, Any]:
    """
    Keep AI useful but safe:
    - Preserve required structure and identity/contact fields.
    - Allow summary/ordering/wording improvements.
    - Prevent accidental section drops.
    """
    if not isinstance(candidate_resume, dict):
        return deepcopy(base_resume)

    normalized = deepcopy(base_resume)

    # profile: keep identity/contact from base, allow summary tweak
    base_profile = base_resume.get("profile", {}) if isinstance(base_resume.get("profile", {}), dict) else {}
    cand_profile = candidate_resume.get("profile", {}) if isinstance(candidate_resume.get("profile", {}), dict) else {}
    merged_profile = deepcopy(base_profile)
    cand_summary = str(cand_profile.get("summary", "")).strip()
    if cand_summary:
        merged_profile["summary"] = cand_summary
    normalized["profile"] = merged_profile

    # links: preserve base unless candidate provides non-empty replacements
    base_links = base_resume.get("links", {}) if isinstance(base_resume.get("links", {}), dict) else {}
    cand_links = candidate_resume.get("links", {}) if isinstance(candidate_resume.get("links", {}), dict) else {}
    merged_links = deepcopy(base_links)
    for k, v in cand_links.items():
        sv = str(v).strip()
        if sv:
            merged_links[k] = sv
    if merged_links:
        normalized["links"] = merged_links

    # list/dict sections: use candidate only if present and non-empty, else keep base
    for key in ("skills", "experience", "projects", "education", "certifications", "additional_info"):
        cand_val = candidate_resume.get(key)
        base_val = base_resume.get(key)
        if key == "skills" and isinstance(cand_val, dict) and len(cand_val) > 0:
            normalized[key] = deepcopy(cand_val)
        elif isinstance(cand_val, list) and len(cand_val) > 0:
            normalized[key] = cand_val
        elif base_val is not None:
            normalized[key] = deepcopy(base_val)

    # hard guard: never return empty key sections
    if not normalized.get("experience"):
        normalized["experience"] = deepcopy(base_resume.get("experience", []))
    if not normalized.get("projects"):
        normalized["projects"] = deepcopy(base_resume.get("projects", []))
    if not normalized.get("skills"):
        normalized["skills"] = deepcopy(base_resume.get("skills", []))

    return normalized


def _apply_role_focus(resume: dict[str, Any], job_description: str) -> dict[str, Any]:
    """Deterministic post-pass to prioritize JD-relevant content without inventing facts."""
    out = deepcopy(resume)
    jd_kw = set(extract_keywords(job_description, limit=48))

    def score_text(text: str) -> int:
        t = (text or "").lower()
        return sum(1 for k in jd_kw if k and k in t)

    # Skills: reorder by relevance, keep full list (do not drop facts).
    skills = out.get("skills", [])
    if isinstance(skills, list) and skills:
        out["skills"] = sorted(skills, key=lambda s: score_text(str(s)), reverse=True)
    elif isinstance(skills, dict):
        for key, items in skills.items():
            if isinstance(items, list) and items:
                skills[key] = sorted(items, key=lambda s: score_text(str(s)), reverse=True)

    # Experience bullets: reorder by relevance + quantified impact.
    exp = out.get("experience", [])
    if isinstance(exp, list):
        for job in exp:
            bullets = job.get("bullets", [])
            if isinstance(bullets, list) and bullets:
                job["bullets"] = sorted(
                    bullets,
                    key=lambda b: (score_text(str(b)), 1 if re.search(r"\d", str(b)) else 0),
                    reverse=True,
                )

    # Projects: reorder by relevance.
    projects = out.get("projects", [])
    if isinstance(projects, list) and projects:
        out["projects"] = sorted(
            projects,
            key=lambda p: score_text(f"{p.get('name','')} {p.get('description','')} {' '.join(p.get('bullets', []))}"),
            reverse=True,
        )

    return out


def _compute_actual_changes(base_resume: dict[str, Any], new_resume: dict[str, Any], claimed_changes: list[str]) -> list[str]:
    """Return human-readable, truthful changes based on actual diff."""
    changes: list[str] = []
    base_summary = str(((base_resume.get("profile") or {}).get("summary", ""))).strip()
    new_summary = str(((new_resume.get("profile") or {}).get("summary", ""))).strip()
    if base_summary != new_summary:
        changes.append("Updated profile summary for job relevance.")

    base_skills = _flatten_skills(base_resume.get("skills") or [])
    new_skills = _flatten_skills(new_resume.get("skills") or [])
    if base_skills != new_skills:
        changes.append("Reordered skills to prioritize job-relevant technologies.")

    base_exp = base_resume.get("experience") or []
    new_exp = new_resume.get("experience") or []
    exp_changed = False
    if isinstance(base_exp, list) and isinstance(new_exp, list):
        for i in range(min(len(base_exp), len(new_exp))):
            b_bullets = [str(x) for x in (base_exp[i].get("bullets") or [])]
            n_bullets = [str(x) for x in (new_exp[i].get("bullets") or [])]
            if b_bullets != n_bullets:
                exp_changed = True
                break
    if exp_changed:
        changes.append("Reordered experience bullets to surface relevant backend/API achievements.")

    base_projects = [p.get("name", "") for p in (base_resume.get("projects") or [])]
    new_projects = [p.get("name", "") for p in (new_resume.get("projects") or [])]
    if base_projects != new_projects:
        changes.append("Reordered projects to align with role requirements.")

    # If no structural change detected, provide transparent note + first claimed note.
    if not changes:
        if claimed_changes:
            changes.append(f"AI suggestion noted: {claimed_changes[0]}")
        changes.append("No major structural changes were applied to preserve factual accuracy.")
    return changes


def _extract_output_object(api_response: dict[str, Any]) -> dict[str, Any]:
    # Some Responses API variants include already-parsed structured JSON.
    if isinstance(api_response.get("output_parsed"), dict):
        return api_response["output_parsed"]
    text = _extract_output_text(api_response)
    return _parse_json_object(text)


def _extract_output_text(api_response: dict[str, Any]) -> str:
    # Responses API text extraction with fallbacks
    out = api_response.get("output", [])
    for item in out:
        for c in item.get("content", []):
            if c.get("type") == "output_text" and c.get("text"):
                return c["text"]
            if c.get("type") == "text" and c.get("text"):
                return c["text"]
            if c.get("type") == "summary_text" and c.get("text"):
                return c["text"]
    # fallback
    if "output_text" in api_response:
        return api_response["output_text"]
    # Some SDK/server variants expose text in nested keys.
    try:
        return api_response["response"]["output"][0]["content"][0]["text"]
    except Exception:
        pass
    raise ValueError("No text in API response")


def _load_openai_api_key() -> str:
    """Load OPENAI_API_KEY from env or local .env files (no extra deps)."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return key

    candidates = [
        Path(".env"),
        Path("src/resume_maker/.env"),
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                if k.strip() != "OPENAI_API_KEY":
                    continue
                v = v.strip().strip("'").strip('"')
                if v:
                    return v
        except Exception:
            continue
    return ""


def _load_gemini_api_key() -> str:
    """Load GEMINI_API_KEY/GOOGLE_API_KEY from env or local .env files."""
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = os.getenv(env_name, "").strip()
        if key:
            return key

    candidates = [
        Path(".env"),
        Path("src/resume_maker/.env"),
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                if k.strip() not in {"GEMINI_API_KEY", "GOOGLE_API_KEY"}:
                    continue
                v = v.strip().strip("'").strip('"')
                if v:
                    return v
        except Exception:
            continue
    return ""


def _cache_key(
    resume_data: dict[str, Any],
    jd: str,
    model: str,
    api_key_available: bool = False,
    use_openai_polish: bool = DEFAULT_USE_OPENAI_POLISH,
    ai_provider: str = "openai",
    local_summary_enabled: bool = OLLAMA_SUMMARY_ENABLED,
) -> str:
    raw = (
        CACHE_SCHEMA_VERSION
        + "\n"
        + json.dumps(resume_data, sort_keys=True)
        + "\n"
        + jd.strip()
        + "\n"
        + model
        + "\n"
        + f"ai={int(api_key_available)}"
        + "\n"
        + f"ollama={int(local_summary_enabled)}:{OLLAMA_MODEL}"
        + "\n"
        + f"polish={int(use_openai_polish)}"
        + "\n"
        + f"provider={ai_provider}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _load_resume_maker_config() -> dict[str, Any]:
    path = Path("config/config.yaml")
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            return {}
        resume_maker_cfg = data.get("resume_maker", {})
        return resume_maker_cfg if isinstance(resume_maker_cfg, dict) else {}
    except Exception:
        return {}


def _resume_maker_use_local_summary() -> bool:
    cfg = _load_resume_maker_config()
    val = cfg.get("use_local_jd_summary")
    if isinstance(val, bool):
        return val
    return OLLAMA_SUMMARY_ENABLED


def _summarize_jd_with_ollama(job_description: str) -> str:
    text = (job_description or "").strip()
    if not text:
        return ""
    prompt = (
        "Summarize this job description for resume tailoring.\n"
        "Output sections only:\n"
        "ROLE:\nMUST_HAVE:\nNICE_TO_HAVE:\nRESPONSIBILITIES:\nKEYWORDS:\n"
        "Use short bullet points and concise keywords.\n\n"
        f"JOB DESCRIPTION:\n{text[:OLLAMA_JD_CHAR_LIMIT]}"
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 260,
            "num_ctx": 1024,
            "num_gpu": 999,
        },
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("response", "")).strip()
    except Exception:
        return ""


def _build_compact_jd(original_jd: str, ollama_summary: str) -> str:
    """
    Build low-token JD input for OpenAI:
    prefer local summary, include a tiny original snippet fallback.
    """
    summary = _normalize_ollama_summary(ollama_summary)
    if summary:
        return summary[:JD_CHAR_LIMIT]
    return (original_jd or "").strip()[:JD_CHAR_LIMIT]


def _normalize_ollama_summary(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    # Remove common noisy headers and collapse whitespace
    s = re.sub(r"(?im)^\s*job description\s*:?\s*$", "", s)
    s = re.sub(r"\r\n?", "\n", s)
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if not lines:
        return ""

    role = ""
    must: list[str] = []
    nice: list[str] = []
    resp: list[str] = []
    keywords: list[str] = []
    section = ""
    for ln in lines:
        low = ln.lower().strip(":")
        if low in {"role", "must_have", "must have", "required skills", "nice_to_have", "nice to have", "preferred skills", "responsibilities", "keywords"}:
            if "role" in low:
                section = "role"
            elif "required" in low or "must" in low:
                section = "must"
            elif "preferred" in low or "nice" in low:
                section = "nice"
            elif "responsibil" in low:
                section = "resp"
            elif "keyword" in low:
                section = "kw"
            continue
        item = ln.lstrip("-• ").strip()
        if not item:
            continue
        if not role and section == "role":
            role = item
        elif section == "must":
            must.append(item)
        elif section == "nice":
            nice.append(item)
        elif section == "resp":
            resp.append(item)
        elif section == "kw":
            keywords.extend([k.strip() for k in item.split(",") if k.strip()])

    if not role:
        role = lines[0][:120]
    # If sections are missing, infer from whole text quickly.
    if not must:
        for k in ("Python", "Django", "DRF", "PostgreSQL", "REST APIs", "Git", "Linux"):
            if re.search(re.escape(k), s, flags=re.I):
                must.append(k)
    if not nice:
        for k in ("Docker", "AWS", "GCP", "Azure", "Celery", "Redis", "CI/CD", "React", "Vue"):
            if re.search(re.escape(k), s, flags=re.I):
                nice.append(k)
    if not resp:
        for frag in lines:
            if any(w in frag.lower() for w in ("develop", "design", "build", "optimiz", "collaborat", "debug", "review")):
                resp.append(frag)
            if len(resp) >= 6:
                break
    if not keywords:
        kw_candidates = must + nice
        seen = set()
        for k in kw_candidates:
            lk = k.lower()
            if lk in seen:
                continue
            seen.add(lk)
            keywords.append(k)

    def explode(items: list[str]) -> list[str]:
        out: list[str] = []
        for it in items:
            # Split overly long combined lines into smaller bullet-friendly chunks.
            parts = re.split(r";|\.\s+|\s\|\s", it)
            for p in parts:
                t = re.sub(r"\s+", " ", p).strip(" .,-")
                if t:
                    out.append(t)
        return out

    def top(items: list[str], n: int = 6) -> list[str]:
        out: list[str] = []
        seen = set()
        for it in explode(items):
            t = re.sub(r"\s+", " ", it).strip(" .")
            if not t:
                continue
            lk = t.lower()
            if lk in seen:
                continue
            seen.add(lk)
            out.append(t)
            if len(out) >= n:
                break
        return out

    must = top(must, 6)
    nice = top(nice, 6)
    resp = top(resp, 6)
    keywords = top(keywords, 10)
    # Remove generic/noisy keyword terms.
    noisy = {
        "full-time", "full time", "completed projects", "job type",
        "work location", "application question(s)", "competitive salary",
    }
    keywords = [k for k in keywords if k.lower() not in noisy]

    parts = [f"ROLE:\n{role}"]
    if must:
        parts.append("MUST_HAVE:\n" + "\n".join(f"- {x}" for x in must))
    if nice:
        parts.append("NICE_TO_HAVE:\n" + "\n".join(f"- {x}" for x in nice))
    if resp:
        parts.append("RESPONSIBILITIES:\n" + "\n".join(f"- {x}" for x in resp))
    if keywords:
        parts.append("KEYWORDS:\n" + ", ".join(keywords))
    return "\n\n".join(parts)


def _default_master_resume() -> dict[str, Any]:
    return {
        "profile": {
            "name": "Your Name",
            "email": "you@example.com",
            "phone": "+1-000-000-0000",
            "location": "Remote",
            "website": "https://portfolio.example.com",
            "summary": "Software engineer building reliable products across backend and AI-assisted workflows.",
        },
        "skills": ["Python", "JavaScript", "SQL", "React", "FastAPI", "Docker", "AWS"],
        "experience": [
            {
                "title": "Software Engineer",
                "company": "Your Company",
                "period": "2023 - Present",
                "bullets": [
                    "Built production APIs and internal tools used by cross-functional teams.",
                    "Automated repetitive workflows and reduced manual effort.",
                    "Collaborated with product and design to ship iterative features quickly.",
                ],
            }
        ],
        "projects": [
            {
                "name": "AI Workflow Assistant",
                "description": "End-to-end assistant for document understanding and retrieval.",
                "bullets": [
                    "Implemented retrieval pipeline and evaluation checks.",
                    "Added caching and observability to reduce latency and cost.",
                ],
            }
        ],
        "education": [{"degree": "B.S. Computer Science", "institution": "University Name", "year": "2026"}],
    }


def _render_resume_html(resume_data: dict[str, Any]) -> str:
    p = resume_data.get("profile", {})
    links = resume_data.get("links", {})
    skills = resume_data.get("skills", [])
    grouped_skills = _get_grouped_skills(resume_data)
    exp = resume_data.get("experience", [])
    projects = resume_data.get("projects", [])
    edu = resume_data.get("education", [])
    certifications = resume_data.get("certifications", [])
    additional_info = resume_data.get("additional_info", [])

    def esc(s: str) -> str:
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    parts: list[str] = []
    parts.append(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>"
        "@page{size:A4;margin:0.5in;}"
        "body{font-family:'Times New Roman',Times,serif;font-size:11.5px;line-height:1.28;color:#111;max-width:8.0in;margin:0 auto;}"
        ".name{font-size:22px;font-weight:700;text-align:center;margin:0;line-height:1.1;}"
        ".contact{font-size:11px;text-align:center;margin:3px 0 8px;color:#222;}"
        ".contact a{color:#1b5f8a;text-decoration:none;}"
        ".section{margin-top:7px;}"
        ".section-title{font-size:13px;font-weight:700;letter-spacing:.35px;text-transform:uppercase;border-bottom:1px solid #222;margin:0 0 4px;line-height:1.2;}"
        ".entry{margin:2px 0 4px;}"
        ".row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;}"
        ".left-strong{font-size:12px;font-weight:700;line-height:1.2;}"
        ".right-strong{font-size:12px;font-weight:700;white-space:nowrap;line-height:1.2;}"
        ".left-italic{font-size:11px;font-style:italic;line-height:1.2;}"
        ".right-italic{font-size:11px;font-style:italic;white-space:nowrap;line-height:1.2;}"
        ".summary{margin:2px 0 4px;}"
        "ul{margin:2px 0 4px 16px;padding:0;}"
        "li{margin:0 0 1px 0;}"
        ".skill-line{margin:1px 0;}"
        ".label{font-weight:700;}"
        ".project-title{font-size:12px;font-weight:700;color:#184f7a;line-height:1.2;}"
        ".project-sub{font-size:11px;line-height:1.2;}"
        "</style></head><body>"
    )
    parts.append(f"<h1 class='name'>{esc(p.get('name',''))}</h1>")
    contact_items = [
        p.get("phone", ""),
        p.get("email", ""),
        links.get("linkedin", ""),
        links.get("github", ""),
        p.get("website", ""),
        p.get("location", ""),
    ]
    contact_items = [esc(x) for x in contact_items if x]
    if contact_items:
        parts.append(f"<div class='contact'>{' | '.join(contact_items)}</div>")

    if p.get("summary"):
        parts.append("<section class='section'><div class='section-title'>Summary</div>")
        parts.append(f"<div class='summary'>{esc(p.get('summary',''))}</div>")
        parts.append("</section>")

    if exp:
        parts.append("<section class='section'><div class='section-title'>Experience</div>")
        for job in exp:
            parts.append("<div class='entry'>")
            parts.append(
                "<div class='row'>"
                f"<div class='left-strong'>{esc(job.get('company',''))}</div>"
                f"<div class='right-strong'>{esc(job.get('location',''))}</div>"
                "</div>"
            )
            parts.append(
                "<div class='row'>"
                f"<div class='left-italic'>{esc(job.get('title',''))}</div>"
                f"<div class='right-italic'>{esc(job.get('period',''))}</div>"
                "</div>"
            )
            bullets = job.get("bullets", [])
            if bullets:
                parts.append("<ul>")
                for b in bullets:
                    parts.append(f"<li>{esc(b)}</li>")
                parts.append("</ul>")
            parts.append("</div>")
        parts.append("</section>")

    if projects:
        parts.append("<section class='section'><div class='section-title'>Projects</div>")
        for pr in projects:
            parts.append("<div class='entry'>")
            header = esc(pr.get("name", ""))
            desc = esc(pr.get("description", ""))
            if desc:
                parts.append(
                    f"<div><span class='project-title'>{header}</span> "
                    f"<span class='project-sub'>| {desc}</span></div>"
                )
            else:
                parts.append(f"<div class='project-title'>{header}</div>")
            bullets = pr.get("bullets", [])
            if bullets:
                parts.append("<ul>")
                for b in bullets:
                    parts.append(f"<li>{esc(b)}</li>")
                parts.append("</ul>")
            parts.append("</div>")
        parts.append("</section>")

    flat_skills = _flatten_skills(skills)
    if grouped_skills or flat_skills:
        parts.append("<section class='section'><div class='section-title'>Technical Skills</div>")
        if grouped_skills:
            for category, items in grouped_skills.items():
                items_text = ", ".join([x for x in items if x])
                if items_text:
                    parts.append(
                        "<div class='skill-line'>"
                        f"<span class='label'>{esc(category)}:</span> {esc(items_text)}"
                        "</div>"
                    )
        else:
            parts.append(f"<div class='skill-line'><span class='label'>Skills:</span> {esc(', '.join(flat_skills))}</div>")
        parts.append("</section>")

    if edu:
        parts.append("<section class='section'><div class='section-title'>Education</div>")
        for e in edu:
            parts.append("<div class='entry'>")
            parts.append(
                "<div class='row'>"
                f"<div class='left-strong'>{esc(e.get('institution',''))}</div>"
                f"<div class='right-strong'>{esc(e.get('location',''))}</div>"
                "</div>"
            )
            parts.append(
                "<div class='row'>"
                f"<div class='left-italic'>{esc(e.get('degree',''))}</div>"
                f"<div class='right-italic'>{esc(e.get('year',''))}</div>"
                "</div>"
            )
            if e.get("bullets"):
                parts.append("<ul>")
                for b in e.get("bullets", []):
                    parts.append(f"<li>{esc(b)}</li>")
                parts.append("</ul>")
            parts.append("</div>")
        parts.append("</section>")

    if certifications:
        parts.append("<section class='section'><div class='section-title'>Certifications</div><ul>")
        for c in certifications:
            parts.append(f"<li>{esc(c)}</li>")
        parts.append("</ul></section>")

    if additional_info:
        parts.append("<section class='section'><div class='section-title'>Additional Information</div><ul>")
        for item in additional_info:
            parts.append(f"<li>{esc(item)}</li>")
        parts.append("</ul></section>")

    parts.append("</body></html>")
    return "".join(parts)
