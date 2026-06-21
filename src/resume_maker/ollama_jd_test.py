from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


DEFAULT_MODEL = "orca-mini:3b-q4_0"
FALLBACK_MODELS = ["gemma3:1b", "gemma3:270m"]
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


PROMPT_TEMPLATE = """You are a job-description analyzer.
Return concise structured output for resume tailoring.

STRICT OUTPUT FORMAT (exact headings, each item on its own line):
ROLE:
<one line>

MUST_HAVE:
- <item>
- <item>

NICE_TO_HAVE:
- <item>
- <item>

RESPONSIBILITIES:
- <item>
- <item>

KEYWORDS:
keyword1, keyword2, keyword3

Rules:
- Max 6 items in each bullet section.
- Keep each bullet short and specific.
- No markdown code fences.
- Do not copy full paragraphs from JD.
- Do not output a "JOB DESCRIPTION" section.

JOB DESCRIPTION:
{job_description}
"""


def call_ollama(model: str, prompt: str, ollama_url: str, timeout: int = 120) -> tuple[str, dict]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 260,
            "num_ctx": 1024,
            # Force/encourage GPU offload on supported systems.
            # Ollama/llama.cpp will still decide exact layer placement based on VRAM.
            "num_gpu": 999,
        },
    }
    req = urllib.request.Request(
        ollama_url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("response", "")).strip()
        if not text:
            raise RuntimeError("Empty response from Ollama.")
        return text, data
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"Ollama HTTP {e.code}: {body[:500]}") from e
    except Exception as e:
        raise RuntimeError(
            f"Failed to call Ollama at {ollama_url}. Is Ollama running and model pulled?"
        ) from e


def read_job_description(arg_text: str | None) -> str:
    if arg_text and arg_text.strip():
        return arg_text.strip()
    print("Paste job description below. Type a new line with only END to finish:\n")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Test local Ollama JD summarization.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help=f"Ollama endpoint (default: {DEFAULT_OLLAMA_URL})")
    parser.add_argument("--text", default="", help="Job description text (optional). If omitted, read from stdin.")
    args = parser.parse_args()

    jd = read_job_description(args.text)
    if not jd:
        print("No job description provided.")
        return 1

    prompt = PROMPT_TEMPLATE.format(job_description=jd[:8000])
    print("\nCalling Ollama... (this can take a bit on first run)\n")
    tried: list[str] = []
    models_to_try = [args.model] + [m for m in FALLBACK_MODELS if m != args.model]
    summary = ""
    raw: dict = {}
    last_err: Exception | None = None
    used_model = ""
    for m in models_to_try:
        tried.append(m)
        try:
            summary, raw = call_ollama(m, prompt, args.ollama_url)
            used_model = m
            break
        except Exception as e:
            last_err = e
            continue
    if not summary:
        print(f"\nERROR: {last_err}")
        print(f"Tried models: {', '.join(tried)}")
        return 2

    print("\n=== OLLAMA SUMMARY ===\n")
    print(normalize_summary(summary))
    print("\n======================\n")
    print(f"model_used={used_model}")
    if isinstance(raw, dict):
        print(
            f"done={raw.get('done')} | eval_count={raw.get('eval_count')} | "
            f"prompt_eval_count={raw.get('prompt_eval_count')}"
        )
    return 0


def normalize_summary(text: str) -> str:
    """Light cleanup to enforce readable sectioned output."""
    s = (text or "").strip()
    if not s:
        return s
    for head in ("ROLE:", "MUST_HAVE:", "NICE_TO_HAVE:", "RESPONSIBILITIES:", "KEYWORDS:"):
        s = s.replace(head, f"\n{head}")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]

    sections: dict[str, list[str]] = {
        "ROLE": [],
        "MUST_HAVE": [],
        "NICE_TO_HAVE": [],
        "RESPONSIBILITIES": [],
        "KEYWORDS": [],
    }
    current = "ROLE"
    for ln in lines:
        t = ln.strip()
        head = t.rstrip(":").upper()
        if head in sections:
            current = head
            continue
        t = t.lstrip("-• ").strip()
        if t:
            sections[current].append(t)

    def explode(items: list[str]) -> list[str]:
        out: list[str] = []
        for it in items:
            for p in __import__("re").split(r";|\.\s+|\s\|\s", it):
                p = " ".join(p.split()).strip(" .,-")
                if p:
                    out.append(p)
        return out

    def uniq_top(items: list[str], n: int) -> list[str]:
        out: list[str] = []
        seen = set()
        for it in explode(items):
            low = it.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(it)
            if len(out) >= n:
                break
        return out

    role = sections["ROLE"][0] if sections["ROLE"] else ""
    must = uniq_top(sections["MUST_HAVE"], 6)
    nice = uniq_top(sections["NICE_TO_HAVE"], 6)
    resp = uniq_top(sections["RESPONSIBILITIES"], 6)
    kws = uniq_top(sections["KEYWORDS"], 10)
    noisy = {"full-time", "full time", "completed projects", "work location", "job type"}
    kws = [k for k in kws if k.lower() not in noisy]

    parts: list[str] = []
    if role:
        parts.append(f"ROLE:\n{role}")
    if must:
        parts.append("MUST_HAVE:\n" + "\n".join(f"- {x}" for x in must))
    if nice:
        parts.append("NICE_TO_HAVE:\n" + "\n".join(f"- {x}" for x in nice))
    if resp:
        parts.append("RESPONSIBILITIES:\n" + "\n".join(f"- {x}" for x in resp))
    if kws:
        parts.append("KEYWORDS:\n" + ", ".join(kws))
    return "\n\n".join(parts).strip()


if __name__ == "__main__":
    raise SystemExit(main())
