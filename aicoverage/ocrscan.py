"""open-code-review（OCR，https://github.com/alibaba/open-code-review）CLI 封装。

OCR 是阿⾥开源的 AI 代码审查⼯具（Apache-2.0）：确定性规则管道 + LLM Agent
混合架构，`ocr review --from <base> --to <head> --format json` 产出⾏级精度
的结构化审查评论。本模块把它接⼊ AIcoverage 扫描轨的 S1 阶段（替代/优先于
⾃研 scan-agent），OCR 产出的评论统⼀转换为本扫描轨的 issue 格式，下游
（gen 复现⽤例 → verify → execute → 四态裁决）链路不变。

安装与配置（运行前提）：
    # 安装（任选其一）
    npm install -g @alibaba-group/open-code-review
    # 或 GitHub Release ⼆进制：opencodereview-linux-amd64 → ~/.local/bin/ocr
    # 依赖 git >= 2.41
    # LLM 配置（OpenAI 兼容⾃定义 provider）
    ocr config set provider my-gateway
    ocr config set custom_providers.my-gateway.url https://<endpoint>/v1
    ocr config set custom_providers.my-gateway.protocol openai
    ocr config set providers.my-gateway.api_key <key>
    ocr config set model <model-name>
    ocr llm test   # 验证连通

OCR 的 JSON 输出 schema 随版本演进，本模块做**宽容解析**（字段名多候选映射），
真实 schema 以配置好 LLM 后的实测输出为准校正（见单测的固定样例）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: OCR 评论 JSON⾥常⻅的"问题列表"容器字段名（按优先级探测）
_LIST_KEYS = ("comments", "findings", "issues", "reviews", "results", "items")

#: 统一 issue 各字段在 OCR 评论对象⾥的候选字段名
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
    """ocr CLI 未安装。"""


class OcrNotConfigured(RuntimeError):
    """ocr 已安装但 LLM 未配置（provider/model）。"""


def is_ocr_available() -> bool:
    return shutil.which("ocr") is not None


def _ocr_config_file() -> Path:
    """OCR 的配置文件路径（未配置时不存在；set 后生成）。多候选位置探测。"""
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
    """粗判 LLM 是否已配置：配置文件存在且含 provider/model 键。

    （OCR 未提供非交互的 config 查询命令，只能探测配置文件；宽容处理——
    探测不到时按"未配置"处理，由调用方降级到 scan-agent 或提示配置。）
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
    """从 OCR JSON 输出中提取评论列表（宽容探测容器字段；顶层数组直接用）。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in _LIST_KEYS:
        v = data.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    # 嵌套一层（如 {"review": {"comments": [...]}}）
    for v in data.values():
        if isinstance(v, dict):
            nested = _extract_comment_list(v)
            if nested:
                return nested
    return []


def parse_ocr_output(raw: str | dict) -> list[dict]:
    """解析 OCR 的 JSON 输出 → 本扫描轨统一 issue 格式。

    统一格式与 scan-agent 的 scan_issues.json 完全一致（issue_id/file/lines/
    severity/category/title/root_cause/trigger_condition/fix_suggestion/
    function/confidence/source），下游 S2-S5 无感知切换。
    """
    data = raw if isinstance(raw, dict) else None
    if data is None:
        text = (raw or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)          # 先整体解析（顶层数组也在此命中）
        except json.JSONDecodeError:
            # OCR json 输出可能混有进度行：退化取第一个 '{'/'[' 到最后一个 '}'/']'
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
            # OCR 评论无显式触发条件字段：留空由 gen 阶段从源码推断
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
    """执行 `ocr review --from base --to head --format json`，返回 (issues, 原始输出)。

    Raises:
        OcrNotAvailable / OcrNotConfigured：由调用方决定降级到 scan-agent。
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
        "--audience", "agent",       # 程序调用：summary only，进度走 stderr
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
