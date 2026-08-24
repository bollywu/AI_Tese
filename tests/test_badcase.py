"""badcase 自回归沉淀单测：解析 / 读侧注入 / 确定性合并（LLM 提议、代码裁决）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.badcase import (  # noqa: E402
    _BASE_PATH, badcase_hint, merge_candidates, parse_badcases,
    project_badcases_path,
)


class TestParse:
    def test_base_md_parses(self):
        """真实 BASE.md（工具级种子库）必须可解析出全部 10 条。"""
        entries = parse_badcases(_BASE_PATH)
        assert len(entries) >= 10
        ids = {e.id for e in entries}
        assert "AICB-001" in ids and "AICB-010" in ids
        # 字段抽取
        e1 = next(e for e in entries if e.id == "AICB-001")
        assert "HookMatcher" in e1.title
        assert e1.category == "sdk-config"
        assert e1.prevention  # 预防规则非空

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_badcases(tmp_path / "nope.md") == []

    def test_garbage_file_returns_empty(self, tmp_path):
        f = tmp_path / "garbage.md"
        f.write_text("随机文本，没有条目格式\n", encoding="utf-8")
        assert parse_badcases(f) == []


class TestHint:
    def test_hint_includes_index(self, tmp_path):
        """BASE 库存在 → 提示必含速查索引表 + gen-quality 摘要。"""

        class _MinCfg:
            workspace = tmp_path

        hint = badcase_hint(_MinCfg())
        assert "badcase 速查" in hint
        assert "AICB-009" in hint           # gen-quality 条目出现在索引
        assert "断言预期值必须来自源码" in hint  # gen-quality 摘要的预防规则
        # 非 gen-quality 条目进索引但不进详情摘要（摘要段以"直接相关的条目摘要"开头）
        summary_part = hint.split("条目摘要：", 1)[-1]
        assert "HookMatcher" not in summary_part

    def test_hint_empty_when_no_base(self, tmp_path, monkeypatch):
        """无任何库（BASE 也不可读）→ 空提示。"""
        import aicoverage.badcase as bc

        class _MinCfg:
            workspace = tmp_path

        monkeypatch.setattr(bc, "_BASE_PATH", tmp_path / "no_base.md")
        assert bc.badcase_hint(_MinCfg()) == ""


class TestMerge:
    def _cand(self, title="测试条目", **kw):
        base = {
            "title": title, "category": "gen-quality",
            "symptom": "s", "root_cause": "r", "prevention": "必须做X",
        }
        base.update(kw)
        return base

    def test_merge_writes_and_roundtrip(self, tmp_path):
        result = merge_candidates(tmp_path, [self._cand()])
        assert len(result["merged"]) == 1 and not result["rejected"]
        # 编号从 BASE 最大号(010)+1 开始，避免与工具级撞号
        assert result["merged"][0]["id"] == "AICB-011"
        # round-trip：写进去的能解析回来
        entries = parse_badcases(project_badcases_path(tmp_path))
        assert len(entries) == 1
        assert entries[0].id == "AICB-011"
        assert entries[0].title == "测试条目"
        assert entries[0].prevention == "必须做X"

    def test_merge_rejects_missing_fields(self, tmp_path):
        bad = self._cand()
        del bad["root_cause"]
        result = merge_candidates(tmp_path, [bad])
        assert not result["merged"]
        assert "缺必填字段" in result["rejected"][0]["reason"]

    def test_merge_rejects_duplicate_title(self, tmp_path):
        """与 BASE 已有条目标题重复（归一化后）→ 拒绝。"""
        dup = self._cand(title="SDK hooks 必须传 HookMatcher dataclass")
        result = merge_candidates(tmp_path, [dup])
        assert not result["merged"]
        assert "重复" in result["rejected"][0]["reason"]

    def test_merge_rejects_duplicate_within_batch(self, tmp_path):
        """同批内重复标题 → 第二条拒绝。"""
        result = merge_candidates(tmp_path, [self._cand("同标题"), self._cand("同标题")])
        assert len(result["merged"]) == 1
        assert len(result["rejected"]) == 1

    def test_merge_rejects_non_object(self, tmp_path):
        result = merge_candidates(tmp_path, ["不是对象"])
        assert result["rejected"][0]["reason"] == "非对象结构"

    def test_merge_bad_entry_does_not_block_good(self, tmp_path):
        """坏条目不阻断好条目（逐条独立裁决）。"""
        bad = self._cand()
        del bad["symptom"]
        result = merge_candidates(tmp_path, [bad, self._cand("好条目")])
        assert len(result["merged"]) == 1
        assert result["merged"][0]["title"] == "好条目"

    def test_merge_twice_ids_increment(self, tmp_path):
        r1 = merge_candidates(tmp_path, [self._cand("第一条")])
        r2 = merge_candidates(tmp_path, [self._cand("第二条")])
        assert r1["merged"][0]["id"] == "AICB-011"
        assert r2["merged"][0]["id"] == "AICB-012"

    def test_hint_after_merge_includes_project_entries(self, tmp_path):
        """沉淀后 badcase_hint 必须能看到新条目（读侧闭环验证）。"""

        class _MinCfg:
            workspace = tmp_path

        merge_candidates(tmp_path, [self._cand("项目特有坑XYZ")])
        hint = badcase_hint(_MinCfg())
        assert "项目特有坑XYZ" in hint

    def test_merge_empty_candidates_no_file(self, tmp_path):
        result = merge_candidates(tmp_path, [])
        assert result["merged"] == []
        assert not project_badcases_path(tmp_path).exists()   # 不创建空库文件


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
