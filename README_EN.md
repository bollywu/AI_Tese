# AIcoverage

> **🌐 Language / 语言切换**: [English](README_EN.md) · [中文（简体）](README.md)

An automated **test-coverage closure loop for any C/C++ project**: **requirement parsing → test generation → local execution → gcov coverage analysis → iterative gap-filling**, until function/branch coverage meets the threshold or early-stop triggers.

> **Acknowledgements**: The call-graph analysis, incremental scanning, knowledge-base construction, and Agent orchestration of this project benefit respectively from [codegraph](https://github.com/colbymchenry/codegraph) (colbymchenry), [open-code-review](https://github.com/alibaba/open-code-review) (Alibaba), [wikirize](https://github.com/tmih06/wikirize) (tmih06), and the **Tencent CodeBuddy team** ([Agent SDK](https://www.codebuddy.ai)). Full credits are listed in the "Third-party open-source dependencies and acknowledgements" section at the end.

## Core Features

- **Out-of-the-box**: drop a single `aicoverage.toml` into your target project root to integrate; supports both CLI programs and "library + driver" projects
- **Fully local execution**: gcc `--coverage` instrumented build → pytest execution → gcov JSON collection, all done by local subprocesses
- **Determinism-first**: build/execution/coverage computation/report assembly are all pure Python; the LLM only makes single-point semantic decisions (generate/review/attribute/scan/adjudicate), with zero hallucination in the execution phase
- **Multi-Agent division of labor**: analyzer (requirement parsing) / coverage (gap root-cause classification N1-N6) / gen (test generation) / verify (static review) / quality (failure attribution) / scan (incremental scan) / kb (knowledge-base construction)
- **MR incremental dual-track loop**: diff extraction (CodeGraph line-range attribution) → call-chain clustering in batches → incremental coverage target + code scanning. The scan track prefers [open-code-review](https://github.com/alibaba/open-code-review) (Alibaba's open-source AI code review, `ocr review --format json`), falling back to the built-in scan-agent when not configured; issues found are auto-converted into reproduction tests and adjudicated in four states (confirmed / false_positive / inconclusive / unobservable)
- **Engineering reliability** (lessons learned from real incidents, all baked into the code):
  - Failure-classified backoff: 429/5xx exponential backoff + jitter + total-duration gate; hallucination detection excludes all identifiable exceptions
  - Liveness timeout: sustained "thinking" without output → judged failed and retried, never hangs indefinitely
  - Context overflow supports compact_hook summary restart (retrying verbatim is pointless)
  - gen-agent is forbidden from executing tests (hard-intercepted by hooks); write directories are whitelisted per agent role
  - System prompts are fully injected into context via AppendSystemPrompt
- **Structured artifact contract**: `loop_state.json` (single source of truth for the state machine) + `events.jsonl` (all agent calls/diagnostics/recovery events, fully replayable)
- **Self-regressing knowledge accumulation**: wiki code knowledge base (optional) + badcase library (quality proposes → deterministic code adjudication stores → gen prompt auto-injects, preventing repeated pitfalls)

## Architecture

```
                    ┌────────────────────────────────────────────────┐
                    │            aicov loop (deterministic FSM)       │
                    └────────────────────────────────────────────────┘
   [0] analyzer-agent      [1] build            [2] baseline
   requirement+test plan →  instrument(--coverage) → existing tests/gcov all-zero list
        │ LLM                   │ deterministic              │ deterministic
        ▼                                              ▼
   ┌── each iteration ───────────────────────────────────────────┐
   │ [a] coverage-agent  LLM: uncovered-function root-cause (N1-N6) │
   │                     + gap-filling suggestions                 │
   │ [b] gen-agent       LLM: generate pytest cases (atomic-building) │
   │ [c] verify-agent    LLM: static review, fail→gen fix loop(≤2)   │
   │ [d] executor         deterministic: pytest + junit + gcov collect│
   │ [e] quality-agent   LLM (when non-PASS): failure attribution/  │
   │                     flaky/suspected bug                         │
   │ [f] state update: delta/threshold/early-stop (coverage_ceiling…)│
   └────────────── loop until threshold met or early-stop ───────────┘
        artifacts: runs/<run_id>/{loop_state.json, events.jsonl,
             iter_N/{manifest,verify_report,junit,execution,coverage,
                     gap_items,quality_report}, loop_final_report.md}
```

## Quick Start

```bash
# 1. Install (requires python≥3.11; LLM phase needs codebuddy-agent-sdk)
cd AIcoverage && pip install -e .

# 2. Generate config + test scaffold in your target project
aicov init --source /path/to/your-project \
           --build-cmd "make CFLAGS='-O0 -g --coverage' LDFLAGS='--coverage'" \
           --binary ./your-app

# 3. (Optional) tune include_globs / thresholds in your-project/aicoverage.toml

# 4. Verify instrumented build
cd /path/to/your-project && aicov build

# 5. Run the full loop (auto-generates HTML coverage report at the end)
aicov loop --yes                       # pure coverage-driven
aicov loop -r "stress-test arg parsing must cover boundary values" --yes   # requirement-driven
aicov loop --with-kb --yes             # build code knowledge base before the loop (recommended first run)

# 5.5 (Optional/recommended) build the code knowledge base separately (wikirize methodology)
aicov kb                                # generates <source>/wiki/ (source-map/entrypoints/
                                        #   flows/contracts/verification…)
# loop agents auto-navigate via wiki (read the map first, then deep-read source; ~45.9% fewer tokens)

# 5.6 badcase self-regression (automatic, no config needed)
#   Sink: quality-agent's per-round failure analysis produces badcase_candidates → deterministic
#         validation/dedup/numbering then merged into <source>/.aicoverage/badcases.md
#   Regression: gen-agent prompt auto-injects known-badcase quick index + gen-quality guard rules
#   Tool-level common pitfalls (10 seeds, real incident postmortems) ship in aicoverage/badcases/BASE.md

# 6. View results
aicov report --list
aicov report LOOP_20260821_160000
```

## Final Report Contents

`runs/<run_id>/loop_final_report.md` is assembled by `finalreport.py` from all on-disk artifacts (layout only, no inference), with six sections:

| Section | Contents | Data source |
|---------|----------|-------------|
| Overview | project/requirement/threshold/conclusion/final coverage (incl. cumulative improvement vs baseline) | `loop_state.json` + last round `coverage.json` |
| 1. Per-round coverage delta | per-round function/branch absolute coverage, Δpp, **newly hit function count**; plus one-line conclusions for "gap analysis / test generation / static review / quality analysis" | `loop_state.json` + per-round `gap_items/manifest/verify_report/quality_report` |
| 2. Test execution results | per-round verdict, case count/pass/fail/error/skip/duration; **failed cases listed one by one with error + quality-agent attribution + fix suggestion** | `junit.xml` + `execution.json` + `quality_report.json` |
| 3. Test case inventory | all test functions listed per file (from on-disk scan), marked "created in iter N / modified in iter N / existed before loop" | scanning `tests/` + per-round `manifest.json` |
| 4. Uncovered functions & reasons | root-cause distribution + per-function table (file:line / function / root cause N1-N6 / verdict / evidence / gap-fill suggestion) | per-round `gap_items.json` + `verdict_unreachable`/`verdict_noop` in `manifest.json` |
| 5. Suspected product bugs | `report_bug` items adjudicated by quality-agent | `quality_report.json` |
| 6. Artifact index | **HTML report URL + open command**, test dir, state machine, event stream, per-round JSON paths | — |

Root-cause codes: **N1** specific runtime env/multi-process/signal · **N2** network peer/protocol interaction · **N3** error path · **N4** requires fine-grained input construction · **N5** dead code/platform-specific/no call site · **N6** directly reachable.

## HTML Coverage Report

Three generation modes:

```bash
# ① Auto-generated at loop end (no extra step)
#    → .aicoverage/reports/coverage_<run_id>/index.html

# ② Run tests and produce the report
aicov coverage --run-tests --html
aicov coverage --run-tests --html ./my_report_dir   # specify output dir

# ③ Generate from existing coverage.json (no test rerun)
aicov html                                  # latest round of the most recent run
aicov html --run-id LOOP_20260821_155342    # specify a run
aicov html --from-json path/to/coverage.json --out ./report
```

The report follows the classic drill-down form of mainstream coverage tools (iframe three-pane + four-column metrics), fully static with zero third-party dependencies — can be copied away or opened via `python3 -m http.server`:

**Layout**: iframe three panes (left collapsible directory-tree nav + draggable splitter + right content area)

**Four-column metric system** (at every level):

| Column | Meaning |
|--------|---------|
| `Function coverage` | % of executed functions (with CSS color bar) |
| `Uncovered functions` | number of unexecuted functions |
| `Condition/decision coverage` | condition/decision coverage (mapped from gcov branch data: share of branch directions hit at least once) |
| `Uncovered conditions/decisions` | number of unhit branch directions |

**Drill-down**: `coverage` (root) → directory → file → **function**

| Page | Contents |
|------|----------|
| `index.html` | iframe frame entry |
| `nav.html` | directory-tree nav (with function-coverage summary per level) |
| `d_<slug>.html` | directory level: four-column metrics for subdirs/files |
| `f_<slug>.html` | **file level: one row per function**, showing that function's own function coverage / condition coverage / uncovered-branch count / execution count, ✔ covered / ✘ uncovered; click a function name to jump to source |
| `s_<slug>.html` | source page: function-definition lines marked ✔/✘, branch lines marked `T`/`F` (unhit directions red), line-by-line coloring (green=executed / red=unexecuted / colorless=non-executable) + per-line execution counts |

> Implementation note: condition/decision coverage is mapped from gcov branch data (share of branch directions hit at least once); color bars are pure CSS (report is plain-text diffable, no binary assets).

## Configuration Reference (aicoverage.toml)

| Section | Field | Description |
|---------|-------|-------------|
| `[project]` | name / language | project name; `c` or `cpp` |
| `[source]` | path / include_globs / exclude_globs | source root; file globs to include in statistics |
| `[build]` | clean_cmd / build_cmd / binary | build command (**must contain `--coverage` instrumentation**; `.gcno` generation is verified after build); artifact path |
| `[test]` | dir / python / timeout | pytest dir; interpreter (auto=probe); overall timeout (>0) |
| `[coverage]` | gcov_bin / func_target / cond_target | gcov executable; threshold lines |
| `[loop]` | max_iter / no_progress_stop | max iterations; consecutive no-growth rounds (early stop) |
| `[llm]` | model / gen_model / max_turns | model configuration |
| `[knowledge]` | kb_dir / badcase_dir / few_shots_dir / prompts_dir | project-specific knowledge resources; prompts_dir can fully override built-in prompts |
| `[guard]` | blocked_commands | extra command blacklist (regex, hard-intercepted by hooks) |

## Generated Test Conventions

`aicov init` generates in the target project:

```
your-project/
├── aicoverage.toml
└── tests/
    ├── conftest.py        # target/src_root fixtures
    └── lib/
        └── harness.py     # atomic-function library (run_binary/local_server/assert_*/print_test_point_box…)
```

**Atomic functions → test-case building blocks**: a test body only does "construct data → call a harness atomic function → feed the result to an assertion atomic function"; when a new verification dimension is needed, extend `harness.py` first, then let the test call it.

**Dual-layer auditability** (doc-header gate added 2026-08-24):
1. **Static**: each `test_*` function's docstring must contain a "description" (one sentence on what behavior is verified) + a "test point" (corresponding source location and branch) — a reviewer can understand each case's purpose **from source alone, without running code**. This is a **deterministic gate** (`aicoverage/docstyle.py`, pure AST parsing, zero LLM tokens), auto-checked by `loop.py` in the verify phase and merged into `verify_report.json` (`EC-07`); a missing field is directly judged fail and looped back to gen-agent to fix.
2. **Runtime**: the three-element execution log (`print_test_point_box()` test-point box / `manual_step()` real observation / assertion expected-vs-observed) ensures a reviewer can re-verify execution details without reading code.

The two complement each other: the static header shows "what is tested", the runtime log shows "how it ran".

## Exit Conditions & Artifact Contract

| status | exit_reason | meaning |
|--------|-------------|---------|
| done | threshold_met | func/cond both meet threshold |
| early_stop | max_iter_reached / coverage_ceiling / execute_fail_loop / gen_no_output / verify_fail_exceeded / build_failed | see loop_state.json |

All artifacts land under the target project's `.aicoverage/` (generated tests under `tests/`):

```
your-project/
├── tests/                              ← generated pytest cases + harness
│   ├── test_*.py
│   └── lib/harness.py
└── .aicoverage/
    ├── runs/<run_id>/
    │   ├── loop_final_report.md         ← final report (coverage evolution/uncovered list/test inventory/suspected bugs)
    │   ├── loop_state.json              ← single source of truth (state machine)
    │   ├── events.jsonl                 ← event stream (task.call/diagnostic/recovery.* …)
    │   ├── analysis.md / test_plan.json  ← requirement-parsing artifacts
    │   ├── build.log
    │   └── iter_N/
    │       ├── gap_items.json           ← coverage-gap root-cause classification (N1-N6)
    │       ├── manifest.json            ← per-round test-output manifest
    │       ├── verify_report.json       ← static review conclusion
    │       ├── junit.xml / pytest.log / execution.json
    │       ├── coverage.json            ← per-round coverage (incl. per-line counts)
    │       └── quality_report.json       ← failure attribution/action_items
    └── reports/coverage_<run_id>/
        ├── index.html                   ← HTML report entry
        └── files/*.html                 ← source line-by-line coloring pages
```

The single source of truth per run is `runs/<run_id>/loop_state.json`; the event stream `events.jsonl` records all agent calls/diagnostics/recovery events (task.call/task.return/diagnostic/recovery.* etc.), fully replayable.

## Directory

```
AIcoverage/
├── aicoverage/
│   ├── config.py         # ProjectConfig (aicoverage.toml)
│   ├── build.py          # instrumented build + .gcno verification
│   ├── gcov.py           # gcov -i -b JSON parsing → CoverageReport
│   ├── executor.py       # deterministic pytest execution + junit + execution.json
│   ├── source.py         # C/C++ function list (ctags-first/regex fallback)
│   ├── runner.py         # AgentRunner (SDK, lazy import)
│   ├── agent_call.py     # failure classification/backoff/hallucination detection/summary restart
│   ├── hooks.py          # security hooks (dangerous commands/out-of-bounds writes/role-based write whitelist)
│   ├── agents.py         # agent definitions + prompt loading
│   ├── loop.py           # main loop state machine (supports target_functions incremental scope)
│   ├── mr_loop.py        # MR incremental loop orchestrator (coverage track + scan track)
│   ├── mrdiff.py         # local git diff extraction
│   ├── diffextract.py    # changed lines → changed functions (CodeGraph line-range attribution)
│   ├── callgraph.py      # CodeGraph wrapper: reverse BFS call chain + batching
│   ├── incremental.py    # coverage scope narrowing view (incremental coverage)
│   ├── scanverify.py     # scan track: scan → reproduction tests → four-state adjudication
│   ├── kb.py             # code knowledge-base construction (wiki, wikirize methodology)
│   ├── badcase.py        # badcase self-regression sink (LLM proposes, code adjudicates)
│   ├── docstyle.py       # test doc-header deterministic gate (description + test point)
│   ├── finalreport.py    # final Markdown report (delta/execution/cases/uncovered-reasons/artifact index)
│   ├── htmlreport.py     # HTML coverage report (source line-by-line coloring)
│   ├── state.py          # loop_state.json
│   ├── observability.py  # events.jsonl
│   ├── templates.py      # scaffolds (config/conftest/harness templates)
│   ├── badcases/BASE.md  # tool-level badcase seed library (shipped)
│   └── prompts/          # agent system prompts (analyzer/coverage/gen/verify/quality/scan/kb)
├── docs/                 # design docs
├── examples/wrk.toml     # wrk example config
└── tests/                # self unit tests
```

## Environment Requirements

- python ≥ 3.11 (zero third-party dependencies in the core deterministic phase)
- gcc ≥ 9 (`gcov -i` JSON intermediate format; gzip output since gcc 12)
- LLM phase: `codebuddy-agent-sdk` (`pip install -e ".[agent]"`) + working CodeBuddy authentication

## Third-party Open-source Dependencies & Acknowledgements

AIcoverage stands on many excellent open-source projects, all acknowledged here:

| Project | Purpose | Author/Organization |
|---------|---------|---------------------|
| [codegraph](https://github.com/colbymchenry/codegraph) | call-graph analysis & diff line-range function attribution (capabilities ②③ of the MR incremental loop) | [colbymchenry](https://github.com/colbymchenry) |
| [open-code-review](https://github.com/alibaba/open-code-review) | incremental code scanning (MR scan-track S1 phase, `ocr review --format json`) | [Alibaba](https://github.com/alibaba) |
| [wikirize](https://github.com/tmih06/wikirize) | code knowledge-base construction methodology (`aicov kb`) | [tmih06](https://github.com/tmih06) |
| [wrk](https://github.com/wg/wrk) | default sample target project (`examples/wrk.toml`) | [wg](https://github.com/wg) |

codegraph and open-code-review are developed and maintained respectively by **colbymchenry** and the **Alibaba** team; wikirize is contributed by **tmih06** — their openness and wisdom enable this project's call-graph analysis, incremental scanning, and knowledge-base capabilities. Hats off to every author.

Additionally, special thanks to the **Tencent CodeBuddy team**: this project builds its multi-Agent orchestration on the CodeBuddy Agent SDK (`codebuddy-agent-sdk`). The Agent framework, runtime, and support they provide are the foundation on which AIcoverage was realized. Thank you, Tencent CodeBuddy team, for your long-term accumulation and open-sourcing in Agent engineering.
