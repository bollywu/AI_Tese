# AIcoverage

> **🌐 语言切换 / Language**：[中文（简体）](README.md) · [English](README_EN.md)

面向**任意 C/C++ 项目**的自动化测试覆盖率闭环：**需求解析 → 测试生成 → 本地执行 → gcov 覆盖率分析 → 迭代补测**，直到函数/分支覆盖率达标或触发早停。

> **Acknowledgements / 致谢**：本项目的调用链分析、增量扫描、知识库构建与 Agent 编排，分别得益于 [codegraph](https://github.com/colbymchenry/codegraph)（colbymchenry）、[open-code-review](https://github.com/alibaba/open-code-review)（Alibaba）、[wikirize](https://github.com/tmih06/wikirize)（tmih06）与**腾讯 CodeBuddy 团队**（[Agent SDK](https://www.codebuddy.ai)）的开源贡献。完整清单见文末「[第三方开源依赖与致谢](#第三方开源依赖与致谢)」。

## 核心特性

- **开箱即用**：一份 `aicoverage.toml` 放进目标项目根即可接入，支持 CLI 程序与"库 + 驱动程序"两种形态
- **全本机运行**：gcc `--coverage` 插桩构建 → pytest 执行 → gcov JSON 采集，全部本地 subprocess 完成
- **确定性优先**：构建/执行/覆盖率计算/报告拼装均为纯 Python 代码；LLM 只做单点语义决策（生成/审查/归因/扫描/裁决），执行环节零幻觉
- **多 Agent 分工**：analyzer（需求解析）/ coverage（缺口根因分类 N1-N6）/ gen（用例生成）/ verify（静态审查）/ quality（失败归因）/ scan（增量扫描）/ kb（知识库构建）
- **MR 增量双轨闭环**：diff 提取（CodeGraph 行区间归因）→ 调用链聚类分批 → 增量覆盖达标 + 代码扫描。扫描轨优先调用 [open-code-review](https://github.com/alibaba/open-code-review)（阿里开源 AI 代码审查，`ocr review --format json`），未配置时自动降级内置 scan-agent；扫描产出的问题自动生成复现用例并做四态裁决（confirmed / false_positive / inconclusive / unobservable）
- **工程化可靠性**（实战事故换来的经验，全部固化在代码里）：
  - 失败分类退避：429/5xx 指数退避 + jitter + 总时长闸门；幻觉判定排除一切可识别异常
  - 活性超时：持续思考不产出 → 判失败重试，不无限挂起
  - 上下文溢出支持 compact_hook 摘要重启（原样重试无意义）
  - gen-agent 禁止执行测试（hooks 硬拦截）；写入目录按 agent 角色白名单
  - system prompt 经 AppendSystemPrompt 完整注入上下文
- **结构化产物契约**：`loop_state.json`（状态机单一真源）+ `events.jsonl`（全部 agent 调用/诊断/恢复事件，可完整回放）
- **知识沉淀自回归**：wiki 代码知识库（可选）+ badcase 库（quality 提议 → 确定性代码裁决入库 → gen prompt 自动注入，防重复踩坑）

## 架构

```
                    ┌────────────────────────────────────────────────┐
                    │            aicov loop（确定性状态机）           │
                    └────────────────────────────────────────────────┘
   [0] analyzer-agent      [1] build            [2] baseline
   需求解析+测试计划   →   插桩构建(--coverage)  →  已有用例跑基线/gcov全0清单
        │ LLM                   │ 确定性               │ 确定性
        ▼                                              ▼
   ┌── 每轮迭代 ──────────────────────────────────────────────┐
   │ [a] coverage-agent  LLM：未覆盖函数根因分类(N1-N6)+补测建议 │
   │ [b] gen-agent       LLM：生成 pytest 用例（原子函数搭积木）  │
   │ [c] verify-agent    LLM：静态审查，fail→gen 修复回环(≤max_verify_retry) │
   │ [d] executor        确定性：pytest + junit + gcov 采集      │
   │ [e] quality-agent   LLM（非 PASS 时）：失败归因/flaky/疑似bug│
   │ [f] 状态更新：delta/达标判定/早停（coverage_ceiling 等）      │
   └──────────────────────── 循环至达标或早停 ──────────────────┘
        产物：runs/<run_id>/{loop_state.json, events.jsonl,
              iter_N/{manifest,verify_report,junit,execution,coverage,
                      gap_items,quality_report}, loop_final_report.md}
```

## 快速开始

```bash
# 1. 安装（需要 python≥3.11；LLM 阶段需 codebuddy-agent-sdk）
cd AIcoverage && pip install -e .

# 2. 在目标项目里生成配置 + 测试脚手架
aicov init --source /path/to/your-project \
           --build-cmd "make CFLAGS='-O0 -g --coverage' LDFLAGS='--coverage'" \
           --binary ./your-app

# 3.（可选）调整 your-project/aicoverage.toml 的 include_globs / 阈值

# 4. 验证插桩构建
cd /path/to/your-project && aicov build

# 5. 跑完整闭环（结束时自动生成 HTML 覆盖率报告）
aicov loop --yes                       # 纯覆盖率驱动
aicov loop -r "压测脚本参数解析需覆盖边界值" --yes   # 需求驱动
aicov loop --with-kb --yes             # 闭环前先构建代码知识库（首次推荐）

# 5.5（可选/推荐）单独构建代码知识库（wikirize 方法论适配）
aicov kb                                # 生成 <source>/wiki/（source-map/entrypoints/
                                        #   flows/contracts/verification…）
# 闭环 agent 自动经 wiki 导航（先读地图再精读源码，基准 -45.9% token）

# 5.6 badcase 自回归（自动，无需配置）
#   沉淀：quality-agent 每轮失败分析产出 badcase_candidates → 确定性代码
#         校验/查重/编号后合并入 <source>/.aicoverage/badcases.md
#   回归：gen-agent prompt 自动注入已知 badcase 速查索引 + gen-quality 预防规则
#   工具级通用坑（10 条种子，真实事故复盘）内置分发于 aicoverage/badcases/BASE.md

# 6. 查看结果
aicov report --list
aicov report LOOP_20260821_160000
```

## 最终报告内容

`runs/<run_id>/loop_final_report.md` 由 `finalreport.py` 汇总全部磁盘产物生成（只排版、不推断），包含六个章节：

| 章节 | 内容 | 数据来源 |
|------|------|---------|
| 概览 | 项目/需求/达标线/结论/最终覆盖率（含相对基线的累计提升） | `loop_state.json` + 最后一轮 `coverage.json` |
| 1. 每轮覆盖率增量 | 每轮函数/分支覆盖绝对值、Δpp、**本轮新命中函数个数**；另附各轮「缺口分析 / 用例生成 / 静态审查 / 质量分析」一句话结论 | `loop_state.json` + 各轮 `gap_items/manifest/verify_report/quality_report` |
| 2. 用例执行结果 | 每轮 verdict、用例数/通过/失败/错误/跳过/耗时；**失败用例逐条列出报错信息 + quality-agent 归因 + 修复建议** | `junit.xml` + `execution.json` + `quality_report.json` |
| 3. 用例清单 | 逐文件列出全部用例函数（磁盘实测），标注「iter N 新建 / iter N 修改 / 闭环前已存在」 | 扫描 `tests/` + 各轮 `manifest.json` |
| 4. 未覆盖函数与原因 | 根因分布统计 + 逐函数表格（文件:行 / 函数 / 根因 N1-N6 / 判定 / 原因证据 / 补测建议） | 各轮 `gap_items.json` + `manifest.json` 的 `verdict_unreachable`/`verdict_noop` |
| 5. 疑似产品缺陷 | quality-agent 判定的 `report_bug` 项 | `quality_report.json` |
| 6. 产物索引 | **HTML 报告地址 + 打开命令**、用例目录、状态机、事件流、各轮 JSON 路径 | — |

根因编码：**N1** 特定运行环境/多进程/信号 · **N2** 网络对端/协议交互 · **N3** 错误路径 · **N4** 需精细输入构造 · **N5** 死代码/平台相关/无调用点 · **N6** 可直接触达。

## HTML 覆盖率报告

三种生成方式：

```bash
# ① 闭环结束自动生成（无需额外操作）
#    → .aicoverage/reports/coverage_<run_id>/index.html

# ② 跑一遍测试并出报告
aicov coverage --run-tests --html
aicov coverage --run-tests --html ./my_report_dir   # 指定输出目录

# ③ 从已有 coverage.json 生成（不重跑测试）
aicov html                                  # 取最近一次 run 的最新一轮数据
aicov html --run-id LOOP_20260821_155342    # 指定 run
aicov html --from-json path/to/coverage.json --out ./report
```

报告采用经典覆盖率工具的层级下钻式形态（iframe 三栏 + 四列指标），纯静态、零第三方依赖，可直接拷走或用 `python3 -m http.server` 打开：

**布局**：iframe 三栏（左侧可折叠目录树导航 + 可拖动分隔条 + 右侧内容区）

**四列指标体系**（每一层级都有）：

| 列 | 含义 |
|----|------|
| `Function coverage` | 已执行函数占比（带 CSS 色条） |
| `Uncovered functions` | 未执行函数数 |
| `Condition/decision coverage` | 条件/决策覆盖（由 gcov 分支数据映射：至少命中一次的分支方向占比） |
| `Uncovered conditions/decisions` | 未命中的分支方向数 |

**层级下钻**：`coverage`（根）→ 目录 → 文件 → **函数**

| 页面 | 内容 |
|------|------|
| `index.html` | iframe 框架入口 |
| `nav.html` | 目录树导航（每层带函数覆盖率摘要） |
| `d_<slug>.html` | 目录层级：子目录/文件的四列指标 |
| `f_<slug>.html` | **文件层级：每个函数一行**，显示该函数自身的函数覆盖 / 条件覆盖 / 未覆盖分支数 / 执行次数，✔ 已覆盖 / ✘ 未覆盖，点击函数名跳源码 |
| `s_<slug>.html` | 源码页：函数定义行标 ✔/✘，分支行标 `T`/`F`（未命中方向标红），逐行着色（绿=已执行 / 红=未执行 / 无色=不可执行）+ 每行执行次数 |

> 实现说明：条件/决策覆盖由 gcov 分支数据映射（至少命中一次的分支方向占比）；色条用纯 CSS 实现（报告可纯文本 diff、无二进制资源）。


## 配置参考（aicoverage.toml）

| 段 | 字段 | 说明 |
|----|------|------|
| `[project]` | name / language | 项目名；`c` 或 `cpp` |
| `[source]` | path / include_globs / exclude_globs | 源码根；参与统计的文件 glob |
| `[build]` | clean_cmd / build_cmd / binary | 构建命令（**必须含 `--coverage` 插桩**，构建后会校验 `.gcno` 生成）；产物路径 |
| `[test]` | dir / python / timeout | pytest 目录；解释器（auto=探测）；整体超时（>0） |
| `[coverage]` | gcov_bin / func_target / cond_target | gcov 可执行文件；达标线 |
| `[loop]` | max_iter / no_progress_stop | 最大迭代；连续无增长轮数（早停） |
| `[llm]` | model / gen_model / max_turns / max_verify_retry | 模型配置；max_turns=单次 agent 最大工具轮次（复杂项目建议 ≥120）；max_verify_retry=verify 失败修复回环次数（复杂项目建议 3） |
| `[knowledge]` | kb_dir / badcase_dir / few_shots_dir / prompts_dir | 按项目自备的知识资源；prompts_dir 可整份覆盖内置 prompt |
| `[guard]` | blocked_commands | 额外命令黑名单（正则，hooks 硬拦截） |

## 生成的测试约定

`aicov init` 会在目标项目生成：

```
your-project/
├── aicoverage.toml
└── tests/
    ├── conftest.py        # target/src_root fixtures
    └── lib/
        └── harness.py     # 原子函数库（run_binary/local_server/assert_*/print_test_point_box…）
```

**原子函数 → 用例搭积木**：用例体只做"构造数据 → 调 harness 原子函数 → 传给断言原子函数"；需要新验证维度时先扩展 `harness.py` 再让用例调用。

**单测通道（e2e 不可达函数转单测）**：某些函数无法通过被测二进制的正常 E2E 流程触达（gap 根因 N1 特定运行环境/多进程/信号、N3 错误路径、N5 死代码/平台相关/无调用点）。此时 gen-agent 会生成 `test_driver_*.c` 直接调用目标函数，用 harness 的 `compile_unit_driver()`（`--coverage` 插桩）+ `run_driver()` 编译运行单测二进制，让 gcov 采集到该函数。因为 gcov 按源码树扫 `.gcno/.gcda`，单测通道与现有采集完全兼容，无需改采集逻辑。单测编译配置见 `aicoverage.toml` 的 `[unittest]` 段（compiler / flags / link_libs / obj_dir）。

**稳定性优化（工程化加固）**：
- **链接失败自愈**：`compile_unit_driver` 遇到 `undefined reference`（缺库）时自动逐个尝试常见库（`-lm`/`-lpthread`/`-lrt`/`-ldl`/`-lz`），成功即用；全部失败则提示在 `[unittest] link_libs` 补全
- **driver 崩溃识别**：`run_driver` 捕获被信号终止（如 SIGSEGV）的 driver，明确提示是 driver 参数构造问题还是被测函数真实缺陷
- **超时保留覆盖**：pytest 整体超时（进程被强杀）也尝试采集 gcov——已执行用例的 `.gcda` 计数不会丢
- **来源可区分**：gcov 采集识别"仅由单测 driver 覆盖、E2E 未命中"的函数（`ut_hit`），HTML 报告文件页以 `UT` 徽标标注，E2E 覆盖与单测覆盖一目了然

**双层可审查性**（2026-08-24 新增文档头门禁）：
1. **静态**：每个 `test_*` 函数的 docstring 必须含"描述"（一句话说明验证什么行为）+ "测试点"（对应源码位置与分支）两个字段——reviewer **不用运行代码、只看源码**就能看懂每个用例的目的。这是**确定性门禁**（`aicoverage/docstyle.py`，纯 AST 解析，零 LLM token），由 `loop.py` 在 verify 阶段自动检查并合并进 `verify_report.json`（`EC-07`），缺字段直接判定 fail，回环给 gen-agent 补齐。
2. **运行时**：执行日志三要素（`print_test_point_box()` 测试点方框 / `manual_step()` 真实观测 / 断言 expected-vs-observed）保证 reviewer 需要复核执行细节时也能不看代码即可复核。

两者互补：静态头看"测什么"，运行日志看"跑得怎样"。

## 退出条件与产物契约

| status | exit_reason | 含义 |
|--------|-------------|------|
| done | threshold_met | func/cond 同时达标 |
| early_stop | max_iter_reached / coverage_ceiling / execute_fail_loop / gen_no_output / verify_fail_exceeded / build_failed | 见 loop_state.json |

产物全部落在被测项目的 `.aicoverage/` 下（生成的用例落在 `tests/`）：

```
your-project/
├── tests/                              ← 生成的 pytest 用例 + harness
│   ├── test_*.py
│   └── lib/harness.py
└── .aicoverage/
    ├── runs/<run_id>/
    │   ├── loop_final_report.md         ← 最终报告（覆盖率演进/未覆盖清单/用例清单/疑似缺陷）
    │   ├── loop_state.json              ← 状态机单一真源
    │   ├── events.jsonl                 ← 事件流（task.call/diagnostic/recovery.* …）
    │   ├── analysis.md / test_plan.json  ← 需求解析产物
    │   ├── build.log
    │   └── iter_N/
    │       ├── gap_items.json           ← 覆盖缺口根因分类（N1-N6）
    │       ├── manifest.json            ← 本轮用例产出清单
    │       ├── verify_report.json        ← 静态审查结论
    │       ├── junit.xml / pytest.log / execution.json
    │       ├── coverage.json            ← 本轮覆盖率（含逐行计数）
    │       └── quality_report.json       ← 失败归因/action_items
    └── reports/coverage_<run_id>/
        ├── index.html                   ← HTML 报告入口
        └── files/*.html                 ← 源码逐行着色页
```

每次 run 的单一真源是 `runs/<run_id>/loop_state.json`，事件流 `events.jsonl` 记录全部 agent 调用/诊断/恢复事件（task.call/task.return/diagnostic/recovery.* 等），可完整回放。

## 目录

```
AIcoverage/
├── aicoverage/
│   ├── config.py         # ProjectConfig（aicoverage.toml）
│   ├── build.py          # 插桩构建 + .gcno 校验
│   ├── gcov.py           # gcov -i -b JSON 解析 → CoverageReport
│   ├── executor.py       # 确定性 pytest 执行 + junit + execution.json
│   ├── source.py         # C/C++ 函数清单（ctags 优先/正则兜底）
│   ├── runner.py         # AgentRunner（SDK，惰性导入）
│   ├── agent_call.py     # 失败分类/退避/幻觉检测/摘要重启
│   ├── hooks.py          # 安全 hooks（危险命令/越界写入/角色化写白名单）
│   ├── agents.py         # agent 定义 + prompt 加载
│   ├── loop.py           # 主闭环状态机（支持 target_functions 增量 scope）
│   ├── mr_loop.py        # MR 增量闭环主编排（覆盖轨 + 扫描轨）
│   ├── mrdiff.py         # 本地 git diff 提取
│   ├── diffextract.py    # 变更行 → 变更函数（CodeGraph 行区间归因）
│   ├── callgraph.py      # CodeGraph 封装：调用链反向 BFS + 分批
│   ├── incremental.py    # 覆盖率 scope 收窄视图（增量覆盖率）
│   ├── scanverify.py     # 扫描轨：扫描 → 复现用例 → 四态裁决
│   ├── kb.py             # 代码知识库构建（wiki，wikirize 方法论）
│   ├── badcase.py        # badcase 自回归沉淀（LLM 提议、代码裁决）
│   ├── docstyle.py       # 用例文档头确定性门禁（描述+测试点）
│   ├── finalreport.py    # 最终 Markdown 报告（增量/执行/用例/未覆盖原因/产物索引）
│   ├── htmlreport.py     # HTML 覆盖率报告（源码逐行着色）
│   ├── state.py          # loop_state.json
│   ├── observability.py  # events.jsonl
│   ├── templates.py      # 脚手架（config/conftest/harness 模板）
│   ├── badcases/BASE.md  # 工具级 badcase 种子库（随分发）
│   └── prompts/          # agent system prompt（analyzer/coverage/gen/verify/quality/scan/kb）
├── docs/                 # 设计文档
├── examples/wrk.toml     # wrk 示例配置
└── tests/                # 自身单测
```

## 环境要求

- python ≥ 3.11（核心确定性阶段零第三方依赖）
- gcc ≥ 9（`gcov -i` JSON 中间格式；gcc 12 起输出 gzip）
- LLM 阶段：`codebuddy-agent-sdk`（`pip install -e ".[agent]"`）+ 可用的 CodeBuddy 认证

## 第三方开源依赖与致谢

AIcoverage 站在众多优秀开源项目之上，在此一并致谢：

| 项目 | 用途 | 作者/组织 |
|------|------|-----------|
| [codegraph](https://github.com/colbymchenry/codegraph) | 调用链分析与 diff 行区间函数归因（MR 增量闭环的②③能力） | [colbymchenry](https://github.com/colbymchenry) |
| [open-code-review](https://github.com/alibaba/open-code-review) | 增量代码扫描（MR 扫描轨 S1 阶段，`ocr review --format json`） | [Alibaba](https://github.com/alibaba) |
| [wikirize](https://github.com/tmih06/wikirize) | 代码知识库构建方法论（`aicov kb`） | [tmih06](https://github.com/tmih06) |
| [wrk](https://github.com/wg/wrk) | 默认示例被测项目（`examples/wrk.toml`） | [wg](https://github.com/wg) |
其中 codegraph 与 open-code-review 分别由 **colbymchenry** 与 **Alibaba** 团队开发维护，wikirize 由 **tmih06** 贡献——它们的开放与智慧让本项目的调用链分析、增量扫描与知识库能力得以实现，向每一位作者致敬。

同时，特别感谢**腾讯 CodeBuddy 团队**：本项目基于 CodeBuddy Agent SDK（`codebuddy-agent-sdk`）构建多 Agent 编排能力，其提供的 Agent 框架、运行环境与配套支持是 AIcoverage 得以落地的基础，感谢腾讯 CodeBuddy 团队在 Agent 工程化方面长期积累与开源分享。
