"""open-code-review 接入单测：JSON 宽容解析 / 可用性探测 / 配置段。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.ocrscan import (  # noqa: E402
    OcrNotAvailable, is_ocr_available, parse_ocr_output,
)


class TestParseOcrOutput:
    def test_comments_list_top_level(self):
        """常见形态：顶层 comments 数组。"""
        raw = json.dumps({"comments": [
            {"file": "src/foo.c", "line": 42, "severity": "High",
             "rule": "NPE", "message": "realloc 失败原指针丢失",
             "suggestion": "用临时变量接收 realloc 返回值"},
        ]})
        issues = parse_ocr_output(raw)
        assert len(issues) == 1
        it = issues[0]
        assert it["issue_id"] == "ISSUE-01"
        assert it["file"] == "src/foo.c" and it["lines"] == "42"
        assert it["severity"] == "high"
        assert it["category"] == "NPE"
        assert "realloc" in it["root_cause"]
        assert it["source"] == "open-code-review"

    def test_findings_key_and_line_range(self):
        raw = json.dumps({"findings": [
            {"path": "a.c", "start_line": 10, "line_end": 15,
             "level": "medium", "category": "thread-safety",
             "summary": "竞态", "body": "check 与 use 之间状态可变"},
        ]})
        issues = parse_ocr_output(raw)
        assert issues[0]["lines"] == "10-15"
        assert issues[0]["severity"] == "medium"

    def test_nested_container(self):
        """嵌套容器（如 {"review": {"comments": [...]}}）也能提取。"""
        raw = json.dumps({"review": {"comments": [
            {"file_path": "b.c", "line_number": 3, "message": "x"},
        ]}})
        assert len(parse_ocr_output(raw)) == 1

    def test_top_level_array(self):
        raw = json.dumps([{"file": "c.c", "line": 1, "message": "y"}])
        assert len(parse_ocr_output(raw)) == 1

    def test_json_with_progress_noise(self):
        """json 输出混有进度行（--audience agent 前的 stdout 噪声）时仍可解析。"""
        raw = 'Reviewing src/foo.c...\n{"comments": [{"file": "d.c", "line": 2, "message": "z"}]}\nDone.\n'
        assert len(parse_ocr_output(raw)) == 1

    def test_empty_and_garbage(self):
        assert parse_ocr_output("") == []
        assert parse_ocr_output("not json at all") == []
        assert parse_ocr_output({"comments": []}) == []
        # 无 file 字段的条目跳过（无法定位的问题对复现闭环无意义）
        assert parse_ocr_output({"comments": [{"message": "no file"}]}) == []

    def test_issue_ids_increment(self):
        raw = json.dumps({"comments": [
            {"file": f"f{i}.c", "line": i, "message": "m"} for i in range(3)
        ]})
        ids = [x["issue_id"] for x in parse_ocr_output(raw)]
        assert ids == ["ISSUE-01", "ISSUE-02", "ISSUE-03"]

    def test_trigger_condition_empty_by_design(self):
        """OCR 评论无显式触发条件字段——留空由 gen 阶段从源码推断。"""
        raw = json.dumps({"comments": [{"file": "e.c", "line": 1, "message": "m"}]})
        assert parse_ocr_output(raw)[0]["trigger_condition"] == ""


class TestAvailability:
    def test_is_ocr_available_bool(self):
        assert isinstance(is_ocr_available(), bool)

    def test_run_review_unavailable_raises(self, tmp_path, monkeypatch):
        import aicoverage.ocrscan as oc

        monkeypatch.setattr(oc, "is_ocr_available", lambda: False)
        with pytest.raises(OcrNotAvailable):
            oc.run_ocr_review(tmp_path, "main", "HEAD")


class TestScanBackendConfig:
    def test_default_auto(self, tmp_path):
        from aicoverage.config import load_config

        p = tmp_path / "aicoverage.toml"
        (tmp_path / "src").mkdir()
        p.write_text(f"""
[project]
name = "demo"
[source]
path = "{tmp_path}"
[build]
build_cmd = "make"
binary = "app"
""", encoding="utf-8")
        import os
        old = os.environ.get("AICOV_CONFIG")
        os.environ["AICOV_CONFIG"] = str(p)
        try:
            cfg = load_config()
            assert cfg.scan_backend == "auto"
        finally:
            if old is None:
                os.environ.pop("AICOV_CONFIG", None)
            else:
                os.environ["AICOV_CONFIG"] = old

    def test_invalid_backend_rejected(self, tmp_path):
        from aicoverage.config import ConfigError, load_config

        p = tmp_path / "aicoverage.toml"
        p.write_text(f"""
[project]
name = "demo"
[source]
path = "{tmp_path}"
[build]
build_cmd = "make"
binary = "app"
[scan]
backend = "nonsense"
""", encoding="utf-8")
        import os
        old = os.environ.get("AICOV_CONFIG")
        os.environ["AICOV_CONFIG"] = str(p)
        try:
            with pytest.raises(ConfigError):
                load_config()
        finally:
            if old is None:
                os.environ.pop("AICOV_CONFIG", None)
            else:
                os.environ["AICOV_CONFIG"] = old


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
