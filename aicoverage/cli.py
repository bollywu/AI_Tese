"""AIcoverage CLI entrypoint.

Subcommands:
  init      generate aicoverage.toml + tests/ harness scaffold in the target project
  build     instrumented build (clean + build_cmd + .gcno verification)
  coverage  collect coverage (optionally run tests first)
  analyze   requirement parsing / source understanding (analyzer-agent, LLM)
  loop      full loop (analyze -> build -> gap -> gen -> verify -> execute -> quality -> ...)
  report    view a run's status / final report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .config import NON_BUILD_LANGUAGES, ProjectConfig, load_config


def main() -> int:
    # Environment-level config (auth etc.) loads before anything: $AICOV_ENV > AIcoverage/.env
    from .env import load_env_file
    load_env_file()

    parser = argparse.ArgumentParser(
        prog="aicov",
        description="AIcoverage — 面向任意 C/C++ 项目的自动化测试覆盖率闭环",
    )
    parser.add_argument("--config", "-c", default=None, help="aicoverage.toml 路径（默认 ./aicoverage.toml）")
    parser.add_argument("--version", action="version", version=f"aicov {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="在目标项目生成配置与测试脚手架")
    p_init.add_argument("--source", required=True, help="被测项目源码根目录")
    p_init.add_argument("--build-cmd", default=None, help="插桩构建命令（须含 --coverage；Go 项目可省略）")
    p_init.add_argument("--binary", default=None, help="构建产物路径（相对源码根；Go 项目可省略）")
    p_init.add_argument("--name", default=None, help="项目名（默认取目录名）")
    p_init.add_argument("--language", default="c", choices=["c", "cpp", "go", "rust", "java"])

    p_build = sub.add_parser("build", help="插桩构建 + .gcno 校验")
    p_build.add_argument("--skip-clean", action="store_true")

    p_cov = sub.add_parser("coverage", help="采集 gcov 覆盖率")
    p_cov.add_argument("--run-tests", action="store_true", help="先跑一遍 tests/ 再采集")
    p_cov.add_argument("--out", default=None, help="coverage.json 输出路径")
    p_cov.add_argument("--html", nargs="?", const="auto", default=None,
                       help="同时生成 HTML 报告（可选指定输出目录，默认 reports/coverage_<时间戳>/）")

    p_html = sub.add_parser("html", help="从 coverage.json 生成 HTML 覆盖率报告")
    p_html.add_argument("--from-json", default=None,
                        help="coverage.json 路径（默认取最近一次 run 的最新 coverage.json）")
    p_html.add_argument("--run-id", default=None,
                        help="指定 run_id，取该 run 最新一轮 coverage.json")
    p_html.add_argument("--out", default=None,
                        help="输出目录（默认 reports/coverage_<时间戳>/）")

    p_analyze = sub.add_parser("analyze", help="需求解析 + 测试计划（LLM）")
    p_analyze.add_argument("--requirement", "-r", default="", help="需求描述")

    p_loop = sub.add_parser("loop", help="完整覆盖率闭环")
    p_loop.add_argument("--requirement", "-r", default="", help="需求描述（业务闭环模式）")
    p_loop.add_argument("--func", type=float, default=None, help="函数覆盖率达标阈值")
    p_loop.add_argument("--cond", type=float, default=None, help="分支覆盖率达标阈值")
    p_loop.add_argument("--max-iter", type=int, default=None)
    p_loop.add_argument("--skip-analyze", action="store_true", help="跳过需求解析（纯覆盖率驱动）")
    p_loop.add_argument("--skip-gap-agent", action="store_true", help="缺口分析降级为确定性裸清单（省 LLM）")
    p_loop.add_argument("--with-kb", action="store_true",
                        help="闭环前先构建代码知识库（wiki/，wikirize 方法论；已建且完整则跳过）")
    p_loop.add_argument("--yes", "-y", action="store_true", help="跳过启动确认")
    p_loop.add_argument("--verbose", "-v", action="store_true")

    p_mr = sub.add_parser("mr", help="MR 增量闭环（diff→调用链分批→增量覆盖达标 + 扫描轨，"
                                     "全部输入来自本地 git，零外部平台依赖）")
    p_mr.add_argument("--base", required=True, help="diff 基准 ref（commit/branch/tag）")
    p_mr.add_argument("--head", default="HEAD", help="diff 目标 ref（默认 HEAD，须已是工作区内容）")
    p_mr.add_argument("--func", type=float, default=None, help="增量函数覆盖率达标阈值")
    p_mr.add_argument("--cond", type=float, default=None, help="增量分支覆盖率达标阈值")
    p_mr.add_argument("--max-iter", type=int, default=None, help="每批闭环最大迭代轮数")
    p_mr.add_argument("--split-by", choices=["file", "chain", "size"], default=None,
                      help="分批策略（默认自动：CodeGraph 可用时 chain，否则 file）")
    p_mr.add_argument("--skip-scan", action="store_true", help="只跑覆盖轨")
    p_mr.add_argument("--skip-coverage", action="store_true", help="只跑扫描轨")
    p_mr.add_argument("--with-kb", action="store_true",
                      help="闭环前先构建代码知识库（wiki/，wikirize 方法论；已建且完整则跳过）")
    p_mr.add_argument("--yes", "-y", action="store_true", help="跳过启动确认")
    p_mr.add_argument("--verbose", "-v", action="store_true")

    p_kb = sub.add_parser("kb", help="构建代码知识库（wiki/，wikirize 方法论适配）——"
                                     "供闭环 agent 导航，降低源码探索成本")
    p_kb.add_argument("--force", action="store_true", help="已有 wiki 也强制重建")

    p_report = sub.add_parser("report", help="查看 run 状态/报告")
    p_report.add_argument("run_id", nargs="?", default=None)
    p_report.add_argument("--list", action="store_true", help="列出全部 run")

    p_mutate = sub.add_parser("mutate", help="变异自检：把被测二进制替换为失效替身重跑某轮新用例，"
                                             "仍 PASS 的用例即假阳性嫌疑（仅 C/C++）")
    p_mutate.add_argument("--run-id", default=None, help="run_id（默认最近一次 LOOP_/MR_ run）")
    p_mutate.add_argument("--iter", type=int, default=None, help="轮次（默认该 run 最新一轮）")

    sub.add_parser("history", help="跨 run 覆盖率演进历史（.aicoverage/history.jsonl）")

    args = parser.parse_args()

    if args.command == "init":
        return _cmd_init(args)

    if args.verbose if hasattr(args, "verbose") else False:
        import os
        os.environ["AICOV_VERBOSE"] = "1"

    cfg = load_config(args.config)

    if args.command == "build":
        return _cmd_build(cfg, args)
    if args.command == "coverage":
        return _cmd_coverage(cfg, args)
    if args.command == "html":
        return _cmd_html(cfg, args)
    if args.command == "analyze":
        return asyncio.run(_cmd_analyze(cfg, args))
    if args.command == "loop":
        return asyncio.run(_cmd_loop(cfg, args))
    if args.command == "mr":
        return asyncio.run(_cmd_mr(cfg, args))
    if args.command == "kb":
        return asyncio.run(_cmd_kb(cfg, args))
    if args.command == "report":
        return _cmd_report(cfg, args)
    if args.command == "mutate":
        return _cmd_mutate(cfg, args)
    if args.command == "history":
        return _cmd_history(cfg)
    return 1


# ── history（跨 run 覆盖历史）─────────────────────────────────────────

def _cmd_history(cfg: ProjectConfig) -> int:
    from .history import load_history, render_history

    entries = load_history(cfg.workspace)
    print(f"▶ 覆盖率演进历史（{cfg.workspace / 'history.jsonl'}，{len(entries)} 次 run）\n")
    print(render_history(entries))
    return 0


# ── mutate（P3 变异自检）─────────────────────────────────────────────

def _cmd_mutate(cfg: ProjectConfig, args) -> int:
    from .mutate import run_mutation_check

    result = run_mutation_check(cfg, run_id=args.run_id, iter_n=args.iter)
    if not result.ok:
        print(f"❌ 变异自检未执行：{result.detail}")
        return 1
    print(f"▶ 变异自检 {result.run_id}/iter_{result.iter_n}：{result.detail}")
    if result.suspicious:
        print(f"\n🔴 假阳性嫌疑用例（对失效二进制仍 PASS，说明未真正验证被测行为）：")
        for n in result.suspicious:
            print(f"  - {n}")
        print(f"\n处置建议：逐条人工复核上述用例的断言是否恒真/无区分度"
              f"（EC-08 恒真门禁未拦住的高阶变体）。报告：见该轮 iter 目录 mutate_report.json")
        return 2
    print("✅ 全部受检用例在变异环境下如预期失败——无假阳性")
    return 0


# ── init ────────────────────────────────────────────────────────────

def _cmd_init(args) -> int:
    from .templates import scaffold

    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        print(f"❌ 源码目录不存在: {source}")
        return 1
    config_path = source / "aicoverage.toml"
    if config_path.exists() and input(f"{config_path} 已存在，覆盖? [y/N] ").strip().lower() != "y":
        print("已取消")
        return 0
    scaffold(source, name=args.name or source.name, build_cmd=args.build_cmd,
             binary=args.binary, language=args.language)
    print(f"✅ 已生成：\n  - {config_path}\n  - {source / 'tests'}/conftest.py\n"
          f"  - {source / 'tests'}/lib/harness.py")
    print(f"\n下一步：\n  cd {source}\n  aicov build && aicov loop --yes")
    return 0


# ── build / coverage ────────────────────────────────────────────────

def _cmd_build(cfg: ProjectConfig, args) -> int:
    if cfg.language in NON_BUILD_LANGUAGES:
        tool = {"go": "go test -coverprofile", "rust": "cargo llvm-cov / tarpaulin",
                "java": "JaCoCo agent"}[cfg.language]
        print(f"✅ {cfg.language} 项目无需插桩构建——{tool} 原生采集覆盖率")
        return 0
    from .build import build as do_build

    result = do_build(cfg, skip_clean=args.skip_clean)
    if result.ok:
        print(f"✅ 构建成功：{result.gcno_count} 个插桩单元，产物 {result.binary}")
        return 0
    print(f"❌ 构建失败：{result.failure_reason}")
    print("构建日志尾部：")
    print("\n".join(result.log.splitlines()[-30:]))
    return 1


def _cmd_coverage(cfg: ProjectConfig, args) -> int:
    from .gcov import collect as gcov_collect, clean_gcda, CoverageReport

    if args.run_tests:
        from .executor import run_tests
        exec_result = run_tests(cfg, cfg.workspace / "standalone")
        print(f"test: verdict={exec_result.verdict} tests={exec_result.tests} "
              f"fail={exec_result.failures} ({exec_result.duration_s:.1f}s)")
        cov_path = exec_result.coverage_path
    elif cfg.language in NON_BUILD_LANGUAGES:
        # Non-build languages have no static collection path (unlike gcov's .gcno
        # inventory baseline): coverage only exists after running the test suite.
        print(f"⚠️ {cfg.language} 项目必须先跑测试才能采集覆盖率，请加 --run-tests")
        return 1
    else:
        clean_gcda(cfg.source_path)
        report = gcov_collect(cfg.source_path, cfg.gcov_bin,
                              include_filter=cfg.include_globs,
                              exclude_filter=cfg.exclude_globs)
        cov_path = Path(args.out) if args.out else cfg.workspace / "coverage.json"
        report.save(cov_path)
    report = CoverageReport.load(cov_path)
    print(f"\n覆盖率（{cov_path}）：")
    print(report.summary_text())

    if args.html:
        out_dir = _html_out_dir(cfg, None if args.html == "auto" else args.html)
        index = _render_html(cfg, report, out_dir)
        print(f"\nHTML 报告：{index}")
    return 0


def _html_out_dir(cfg: ProjectConfig, explicit: str | None) -> Path:
    from datetime import datetime
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_absolute() else (cfg.source_path / p)
    return cfg.reports_dir / f"coverage_{datetime.now():%Y%m%d_%H%M%S}"


def _render_html(cfg: ProjectConfig, report, out_dir: Path, run_id: str = "",
                 extra_links: dict[str, str] | None = None) -> Path:
    from .htmlreport import generate
    return generate(report, out_dir, source_root=cfg.source_path,
                    project_name=cfg.display_name, run_id=run_id,
                    extra_links=extra_links)


def _cmd_html(cfg: ProjectConfig, args) -> int:
    from .gcov import CoverageReport

    cov_path: Path | None = None
    run_id = ""
    if args.from_json:
        cov_path = Path(args.from_json).expanduser()
    else:
        # take the latest round's coverage.json from the given run (or the most recent run)
        run_dirs = []
        if cfg.runs_dir.exists():
            run_dirs = sorted((d for d in cfg.runs_dir.iterdir() if d.is_dir()),
                              key=lambda d: d.stat().st_mtime, reverse=True)
        if args.run_id:
            run_dirs = [d for d in run_dirs if d.name == args.run_id]
            if not run_dirs:
                print(f"❌ 未找到 run: {args.run_id}")
                return 1
        for d in run_dirs:
            covs = sorted(d.glob("iter_*/coverage.json"),
                          key=lambda p: int(p.parent.name.split("_")[1]))
            if covs:
                cov_path, run_id = covs[-1], d.name
                break
        if cov_path is None:
            fallback = cfg.workspace / "coverage.json"
            if fallback.exists():
                cov_path = fallback
    if cov_path is None or not cov_path.exists():
        print("❌ 未找到 coverage.json。先执行 `aicov coverage --run-tests` 或 `aicov loop`。")
        return 1

    report = CoverageReport.load(cov_path)
    out_dir = _html_out_dir(cfg, args.out)
    links = {}
    if run_id:
        report_md = cfg.runs_dir / run_id / "loop_final_report.md"
        if report_md.exists():
            import os
            links["闭环报告 (Markdown)"] = os.path.relpath(report_md, out_dir)
    index = _render_html(cfg, report, out_dir, run_id=run_id, extra_links=links)
    print(f"数据源：{cov_path}")
    print(f"HTML 报告：{index}")
    print(f"  函数 {report.func_pct:.2f}% / 分支 {report.cond_pct:.2f}% / 行 {report.line_pct:.2f}%")
    return 0


# ── analyze / loop（LLM） ───────────────────────────────────────────

async def _cmd_analyze(cfg: ProjectConfig, args) -> int:
    from .agent_call import call_agent
    from .runner import AgentRunner
    from . import state as st, observability as obs
    from .loop import _prompt_analyze

    run_id = st.gen_run_id("ANALYZE")
    run_dir = cfg.runs_dir / run_id
    files = cfg.source_files()
    files_preview = "\n".join(p.relative_to(cfg.source_path).as_posix() for p in files[:80])
    runner = AgentRunner(cfg, run_dir=run_dir)
    result = await call_agent(
        runner, run_id, "analyzer-agent",
        _prompt_analyze(cfg, run_dir, args.requirement, files_preview),
        runs_dir=cfg.runs_dir, stage="analyze", max_retries=2,
    )
    plan = run_dir / "test_plan.json"
    if plan.exists():
        print(f"\n✅ 分析产物：{run_dir / 'analysis.md'}\n          {plan}")
        return 0 if result.success else 1
    print(f"⚠️ analyzer 未产出 test_plan.json（详见 {run_dir}）")
    return 1


async def _maybe_build_kb(cfg: ProjectConfig, args, *, interactive: bool) -> bool:
    """Knowledge-base build choice before the loop (user requirement: choose to build KB
    before running full functionality).

    Rules:
    - --with-kb: always build first (run_kb_build skips internally if already complete)
    - interactive (no --yes) and wiki absent: ask whether to build first (default recommend y)
    - non-interactive and no --with-kb: just print a hint (CI-friendly, non-blocking); use
      `aicov kb` or --with-kb
    Returns:
        True = built (or already exists); False = not built (loop runs without wiki navigation).
    """
    from .kb import run_kb_build, wiki_ready
    if getattr(args, "with_kb", False):
        await run_kb_build(cfg)
        return True
    if wiki_ready(cfg):
        return True
    if interactive:
        print(f"\n检测到尚未构建代码知识库（{cfg.source_path}/wiki/ 不存在）。")
        print("知识库（wikirize 方法论）可让后续 agent 先读地图再精读源码：")
        print("  基准数据：agent 源码探索 -45.9% token / -28.8% 耗时")
        print("是否先构建知识库再跑闭环? [Y/n] ", end="")
        if input().strip().lower() in ("", "y", "yes"):
            await run_kb_build(cfg)
            return True
        print("已跳过知识库构建（后续可用 `aicov kb` 单独构建）\n")
        return False
    print(f"ℹ️ 提示：尚未构建代码知识库（可先跑 `aicov kb` 或闭环时加 --with-kb，"
          f"让 agent 以 wiki 导航降低探索成本）")
    return False


async def _cmd_loop(cfg: ProjectConfig, args) -> int:
    from .loop import run_loop

    if not args.yes:
        print(f"即将启动闭环：项目={cfg.name} func≥{args.func or cfg.func_target}% "
              f"cond≥{args.cond or cfg.cond_target}% max_iter={args.max_iter or cfg.max_iter}")
        print("将真实调用 LLM 生成/修改用例并执行 pytest，继续? [y/N] ", end="")
        if input().strip().lower() != "y":
            print("已取消")
            return 0
        await _maybe_build_kb(cfg, args, interactive=True)
    else:
        await _maybe_build_kb(cfg, args, interactive=False)
    final = await run_loop(
        cfg,
        requirement=args.requirement,
        func_target=args.func,
        cond_target=args.cond,
        max_iter=args.max_iter,
        skip_analyze=args.skip_analyze,
        skip_gap_agent=args.skip_gap_agent,
        interactive=not args.yes,
    )
    return 0 if final.get("status") == "done" else 2


# ── mr ───────────────────────────────────────────────────────────────

async def _cmd_mr(cfg: ProjectConfig, args) -> int:
    from .mr_loop import run_mr_loop

    if not args.yes:
        print(f"即将启动 MR 增量闭环：项目={cfg.name} base={args.base} head={args.head}")
        print(f"  覆盖轨：调用链分批 + 增量覆盖率达标（func≥{args.func or cfg.func_target}% "
              f"cond≥{args.cond or cfg.cond_target}%）")
        print("  扫描轨：scan-agent 本地聚焦扫描 + 复现用例 + 四态裁决")
        print("将真实调用 LLM 并执行 pytest，继续? [y/N] ", end="")
        if input().strip().lower() != "y":
            print("已取消")
            return 0
        await _maybe_build_kb(cfg, args, interactive=True)
    else:
        await _maybe_build_kb(cfg, args, interactive=False)
    summary = await run_mr_loop(
        cfg,
        base_ref=args.base,
        head_ref=args.head,
        func_target=args.func,
        cond_target=args.cond,
        max_iter=args.max_iter,
        split_by=args.split_by,
        skip_scan=args.skip_scan,
        skip_coverage=args.skip_coverage,
    )
    return 0 if summary.get("status") == "done" else 2


async def _cmd_kb(cfg: ProjectConfig, args) -> int:
    from .kb import run_kb_build

    result = await run_kb_build(cfg, force=getattr(args, "force", False))
    if result.get("status") == "ok":
        print(f"\n后续 `aicov loop` / `aicov mr` 的 agent 将自动通过 wiki 导航"
              f"（{cfg.source_path}/wiki/agent-quickstart.md 优先）")
    return 0 if result.get("status") in ("ok", "skipped") else 2


# ── report ──────────────────────────────────────────────────────────

def _cmd_report(cfg: ProjectConfig, args) -> int:
    if args.list or not args.run_id:
        for d in sorted(cfg.runs_dir.iterdir()) if cfg.runs_dir.exists() else []:
            state_file = d / "loop_state.json"
            if state_file.exists():
                s = json.loads(state_file.read_text(encoding="utf-8"))
                print(f"{s.get('run_id', d.name):26s} {s.get('status', '?'):12s} "
                      f"{s.get('exit_reason', '')}")
            elif (d / "analysis.md").exists():
                print(f"{d.name:26s} analyze-only")
        return 0
    state_file = cfg.runs_dir / args.run_id / "loop_state.json"
    if not state_file.exists():
        report = cfg.runs_dir / args.run_id / "loop_final_report.md"
        if report.exists():
            print(report.read_text(encoding="utf-8"))
            return 0
        print(f"❌ 未找到 run: {args.run_id}")
        return 1
    print(json.dumps(json.loads(state_file.read_text(encoding="utf-8")),
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
