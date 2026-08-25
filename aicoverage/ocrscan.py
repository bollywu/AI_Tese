"""open-code-review (OCR, https://github.com/alibaba/open-code-review) CLI wrapper.

OCR is Alibaba's open-source AI code-review tool (Apache-2.0): a deterministic rule pipeline +
LLM Agent hybrid architecture; `ocr review --from <base> --to <head> --format json` produces
line-precise structured review comments. This module wires it into AIcoverage's scan-track S1
phase (replacing / prioritized over the built-in scan-agent). OCR comments are uniformly
converted into this scan track's issue format, so downstream (gen repro case -> verify ->
execute -> four-state adjudication) stays unchanged.

Install & config (prerequisites):
    # install (pick one)
    npm install -g @alibaba-group/open-code-review
    # or GitHub Release binary: opencodereview-linux-amd64 -> ~/.local/bin/ocr
    # requires git >= 2.41
    # LLM config (OpenAI-compatible custom provider)
    ocr config set provider my-gateway
    ocr config set custom_providers.my-gateway.url https://<endpoint>/v1
    ocr config set custom_providers.my-gateway.protocol openai
    ocr config set providers.my-gateway.api_key <key>
    ocr config set model <model-name>
    ocr llm test   # verify connectivity

OCR's JSON output schema evolves across versions, so this module does **lenient parsing**
(multi-candidate field-name mapping); the real schema is calibrated against measured output
after LLM config (see the fixed samples in unit tests).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: Common container field names for OCR's "issue list" in comment JSON (probed by priority)
_LIST_KEYS = ("comments", "findings", "issues", "reviews", "results", "items")

#: Candidate field names for each unified-issue field inside an OCR comment object
_FIELD_CANDIDATES = {
    "file": ("file", "file_path", "path", "filename"),
    "line": ("line", "line_number", "start_line", "line_start"),
    "end_line": ("end_line", "line_end", "stop_line"),
    "severity": ("severity", "priority", "level"),
    "category": ("category", "rule", "rule_id", "rule_name", "type", "check"),
    "title": ("title", "summary", "rule_name"),
    "root_cause": ("message", "description", "body", "comment", "detail", "content"),
    "suggestion": ("suggestion", "fix", "fix_suggestion", "recommendation"),
    "function": ("function", "symbol", "func"),
}


class OcrNotAvailable(RuntimeError):
    """ocr CLI not installed."""


class OcrNotConfigured(RuntimeError):
    """ocr installed but LLM not configured (provider/model)."""


def is_ocr_available() -> bool:
    return shutil.which("ocr") is not None


def _ocr_config_file() -> Path:
    """Path to OCR's config file (absent when unconfigured; created by `set`). Probes multiple locations."""
    import os
    home = Path(os.environ.get("HOME", str(Path.home())))
    candidates = [
        home / ".config" / "opencodereview" / "config.json",
        home / ".opencodereview" / "config.json",
        home / ".config" / "ocr" / "config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def is_ocr_configured() -> bool:
    """Roughly judge whether the LLM is configured: config file exists and has provider/model keys.

    (OCR offers no non-interactive config-query command, so only the config file can be probed;
    lenient handling -- when undetectable, treat as "unconfigured" and let the caller fall back
    to scan-agent or prompt for config.)
    """
    f = _ocr_config_file()
    if not f.exists():
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    text = json.dumps(data)
    return ("provider" in data or "llm" in data
            or "custom_providers" in data or "providers" in text)


def _pick(obj: dict, field: str) -> Any:
    for key in _FIELD_CANDIDATES[field]:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _extract_comment_list(data: Any) -> list[dict]:
    """Extract the comment list from OCR JSON output (lenient container probing; top-level array used directly)."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in _LIST_KEYS:
        v = data.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    # nest one level (e.g. {"review": {"comments": [...]}})
    for v in data.values():
        if isinstance(v, dict):
            nested = _extract_comment_list(v)
            if nested:
                return nested
    return []


def parse_ocr_output(raw: str | dict) -> list[dict]:
    """Parse OCR's JSON output -> this scan track's unified issue format.

    The unified format is identical to scan-agent's scan_issues.json
    (issue_id/file/lines/severity/category/title/root_cause/trigger_condition/
    fix_suggestion/function/confidence/source), so downstream S2-S5 switch without awareness.
    """
    data = raw if isinstance(raw, dict) else None
    if data is None:
        text = (raw or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)          # try whole-parse first (top-level array also hits here)
        except json.JSONDecodeError:
            # OCR json output may mix in progress lines: degrade to taking the first '{'/'['
            # through the last '}'/']'
            for open_ch, close_ch in (("{", "}"), ("[", "]")):
                start, end = text.find(open_ch), text.rfind(close_ch)
                if start >= 0 and end > start:
                    try:
                        data = json.loads(text[start:end + 1])
                        break
                    except json.JSONDecodeError:
                        continue
            if data is None:
                return []

    issues: list[dict] = []
    for i, c in enumerate(_extract_comment_list(data), 1):
        file = _pick(c, "file")
        if not file:
            continue
        line = _pick(c, "line")
        end_line = _pick(c, "end_line")
        lines = str(line) if line is not None else ""
        if end_line is not None and str(end_line) != lines:
            lines = f"{line}-{end_line}"
        root_cause = _pick(c, "root_cause") or ""
        suggestion = _pick(c, "suggestion") or ""
        issues.append({
            "issue_id": f"ISSUE-{i:02d}",
            "file": str(file),
            "lines": lines,
            "severity": str(_pick(c, "severity") or "medium").lower(),
            "category": str(_pick(c, "category") or "ocr_review"),
            "title": str(_pick(c, "title") or "OCR 审查发现")[:120],
            "root_cause": str(root_cause),
            # OCR comments have no explicit trigger-condition field: leave empty; gen infers
            # it from the source
            "trigger_condition": "",
            "fix_suggestion": str(suggestion),
            "function": str(_pick(c, "function") or ""),
            "confidence": "medium",
            "source": "open-code-review",
        })
    return issues


def run_ocr_review(
    source_path: Path, base_ref: str, head_ref: str, *,
    output_path: Path | None = None, timeout: int = 600,
) -> tuple[list[dict], str]:
    """Run `ocr review --from base --to head --format json`; returns (issues, raw output).

    Raises:
        OcrNotAvailable / OcrNotConfigured: caller decides whether to fall back to scan-agent.
    """
    if not is_ocr_available():
        raise OcrNotAvailable("ocr CLI 未安装（npm i -g @alibaba-group/open-code-review "
                               "或 GitHub Release 二进制）")
    if not is_ocr_configured():
        raise OcrNotConfigured("ocr 已安装但 LLM 未配置（ocr config set provider/model）")

    out = output_path or (source_path / ".aicoverage" / "ocr_review.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ocr", "review",
        "--from", base_ref, "--to", head_ref,
        "--format", "json", "--output", str(out),
        "--audience", "agent",       # programmatic call: summary only; progress via stderr
        "--repo", str(source_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            cwd=str(source_path))
    raw = ""
    if out.exists():
        try:
            raw = out.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
    if result.returncode != 0 and not raw.strip():
        raise RuntimeError(
            f"ocr review 失败（rc={result.returncode}）: "
            f"{(result.stderr or result.stdout)[-500:]}")
    return parse_ocr_output(raw), raw
