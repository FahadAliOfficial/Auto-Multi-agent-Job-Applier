from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from src.resume_maker.engine import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_USE_OPENAI_POLISH,
    GEMINI_RESUME_MAKER_MODEL,
    JD_CHAR_LIMIT,
    LOW_COST_MODE,
    RESUME_MAKER_MODEL,
    _load_gemini_api_key,
    _load_openai_api_key,
    export_tailored_resume_files,
    load_master_resume,
    render_resume_html,
    render_resume_markdown,
    save_master_resume,
    tailor_resume,
)
from src.utils.logger import setup_logger

logger = setup_logger("resume_maker_app")


def create_resume_maker_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )

    @app.get("/")
    def index():
        master = load_master_resume()
        openai_key_present = bool(_load_openai_api_key())
        gemini_key_present = bool(_load_gemini_api_key())
        return render_template(
            "resume_maker.html",
            master_json=json.dumps(master, indent=2),
            low_cost_mode=LOW_COST_MODE,
            jd_char_limit=JD_CHAR_LIMIT,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            openai_model_name=RESUME_MAKER_MODEL,
            gemini_model_name=GEMINI_RESUME_MAKER_MODEL,
            default_use_openai_polish=(DEFAULT_USE_OPENAI_POLISH or openai_key_present or gemini_key_present),
        )

    @app.post("/api/master/save")
    def api_save_master():
        payload = request.get_json(silent=True) or {}
        master = payload.get("master_resume")
        if not isinstance(master, dict):
            return jsonify({"ok": False, "error": "master_resume must be an object"}), 400
        save_master_resume(master)
        return jsonify({"ok": True})

    @app.post("/api/tailor")
    def api_tailor():
        payload = request.get_json(silent=True) or {}
        master = payload.get("master_resume")
        jd = str(payload.get("job_description", "")).strip()
        ai_provider = str(payload.get("ai_provider", "openai")).strip().lower()
        if ai_provider not in {"openai", "gemini"}:
            ai_provider = "openai"
        default_model = GEMINI_RESUME_MAKER_MODEL if ai_provider == "gemini" else RESUME_MAKER_MODEL
        model = str(payload.get("model", default_model)).strip() or default_model
        use_openai_polish = bool(payload.get("use_openai_polish", DEFAULT_USE_OPENAI_POLISH))
        if not isinstance(master, dict):
            return jsonify({"ok": False, "error": "master_resume must be an object"}), 400
        result = tailor_resume(
            master,
            jd,
            model=model,
            use_openai_polish=use_openai_polish,
            ai_provider=ai_provider,
        )
        ai_call_succeeded = not use_openai_polish or not any(
            "skipped in low-cost mode due to error" in str(c).lower()
            or "not set; used heuristic-only tailoring" in str(c).lower()
            for c in result.changes
        )
        logger.info(
            "Tailor request provider=%s model=%s polish=%s cached=%s ai_call_succeeded=%s",
            ai_provider,
            model,
            use_openai_polish,
            result.cached,
            ai_call_succeeded,
        )
        markdown = render_resume_markdown(result.tailored_resume)
        html_preview = render_resume_html(result.tailored_resume)
        return jsonify(
            {
                "ok": True,
                "tailored_resume": result.tailored_resume,
                "markdown": markdown,
                "html_preview": html_preview,
                "changes": result.changes,
                "model": result.model,
                "ai_provider": ai_provider,
                "ai_call_succeeded": ai_call_succeeded,
                "cached": result.cached,
                "estimated_input_tokens": result.estimated_input_tokens,
                "estimated_output_tokens": result.estimated_output_tokens,
                "cache_key": result.cache_key,
                "low_cost_mode": LOW_COST_MODE,
                "jd_char_limit": JD_CHAR_LIMIT,
                "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "use_openai_polish": use_openai_polish,
            }
        )

    @app.post("/api/export")
    def api_export():
        payload = request.get_json(silent=True) or {}
        resume = payload.get("tailored_resume")
        job_title = str(payload.get("job_title_hint", "")).strip()
        if not isinstance(resume, dict):
            return jsonify({"ok": False, "error": "tailored_resume must be an object"}), 400
        files = export_tailored_resume_files(resume, job_title_hint=job_title)
        return jsonify({"ok": True, "files": files})

    return app
