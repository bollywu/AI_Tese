> **文档状态**：设计与实施记录（功能已全部实现并经真实项目端到端验证）

# AIcoverage MR 增量闭环设计

## 0. 能力概览

MR 增量闭环把「全量覆盖率闭环」升级为「代码变更粒度的双轨闭环」：

| # | 能力 | 输入 | 输出 |
|---|------|------|------|
| ① | 从代码变更拿到 diff | 本地 git ref（commit/branch/tag） | 变更文件 + 变更行区间 |
| ② | 结合 CodeGraph 解析调用链路 | 变更函数 | 每个变更函数「从入口到自身」的调用路径 |
| ③ | 设计用例覆盖增量代码，达到**增量覆盖率达标** | ①② 的产出 | 测试用例 + 增量覆盖率报告 |
| ④ | 对 diff 做代码扫描，对扫描出的问题设计测试用例 | 变更 diff | 复现/证伪用例 + 四态裁决 |

③④ 汇总为一份 MR 报告。**完全本地、零外部平台依赖**——适用于 GitHub / GitLab /
任意私有仓库 clone 到本地的场景。

## 1. 总体架构

```
                          MR / commit range（本地 git）
                                 │
                    ┌────────────┴────────────┐
                    │   [M0] diff 提取          │  确定性：git diff -U0
                    │   + CodeGraph 行区间归因  │  → changed_functions.json
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   [M1] 调用链聚类分批     │  CodeGraph 反向 BFS
                    │   （file/chain/size）    │  → diff_batches.json
                    └────────────┬────────────┘
              ┌──────────────────┴──────────────────┐
    ┌─────────▼─────────┐                 ┌───────────▼───────────┐
    │  覆盖轨 (Track-A)   │                 │     扫描轨 (Track-B)     │
    │ [M2] 逐批复用主闭环  │                 │ [M3] scan-agent 聚焦扫描 │
    │  scope 收窄到本批    │                 │  → gen 复现用例(正向断言)│
    │  变更函数，追求增量   │                 │  → verify → execute     │
    │  func/cond 达标     │                 │  → 四态裁决              │
    └─────────┬─────────┘                 └───────────┬───────────┘
              └──────────────────┬──────────────────┘
                    ┌────────────▼────────────┐
                    │   [M4] 统一 MR 报告       │  mr_final_report.md
                    └─────────────────────────┘
```

**核心设计原则**（延续 AIcoverage 铁律）：确定性优先——diff 提取、调用链 BFS、
覆盖率计算、报告拼装全部是纯 Python 代码；LLM 只做语义判断（生成用例、审查、
扫描问题识别、失败归因、裁决辅助）。两轨共享同一套原子设施（gen/verify/
execute/quality 调用与 hooks 安全约束），只是输入范围与判定逻辑不同。

## 2. 关键设计决策

### 2.1 diff 提取：CodeGraph 行区间反查，不用 hunk 正则

`git diff -U0` 只取"改了哪些行"，**函数归因靠 CodeGraph 行区间反查**
（`functions_covering_lines`），不信任 hunk header 的函数名提示（它可能
对应上一个函数、可能丢类限定名）。每个变更函数标注归因可信度：

- `codegraph_range`：行区间命中唯一函数 → 可信
- `conflict`：CodeGraph 结果与 hunk header 交叉校验不一致 → 不入闭环
  分母，转人工复核
- 改动行不在任何已索引函数内（全局变量/宏/注释区）→ `unresolved_files`

### 2.2 调用链聚类：入口可配置

反向 BFS（`trace_to_entrypoints`）从目标函数沿"谁调用了我"回溯到配置的
入口锚点（`[codegraph].entrypoints`，通常是 `main`）。查不到入口路径的
函数标记 unreachable（疑似死代码/未接线）——**单独成批且在 gen 上下文
明确提示不伪造用例**，而不是盲目生成"假覆盖"用例。

分批策略三档：`file`（按文件，最简单）/ `chain`（调用链聚类，默认）/
`size`（固定数量兜底）。

### 2.3 增量覆盖率：函数级 scope 收窄

增量 func_pct = 变更函数集合中"整个函数体"被执行过的比例（含连带回归
语义：函数改了几行，其余分支也应验证未被破坏）。通过
`incremental.scope_report()` 从全量 CoverageReport 收窄出子集视图，复用
现有 `delta()` 与达标判断，不新增判定逻辑。

边界处理：
- 目标函数不在覆盖率数据中（未插桩/已删除/名称不一致）→ `missing_targets`
  显式报告，不与"存在但未执行"混淆
- scope 内无可测分支 → cond 视为满足（vacuous，显式标注），避免
  "达标但显示 0%"的自相矛盾
- 函数全覆盖但分支未达标 → 以"含未命中分支的函数"构造 gap 继续

### 2.4 扫描轨：本地 scan-agent 单通道

scan-agent（第 6 个 agent）对变更函数及其调用链上下文做聚焦式语义扫描
（内存安全/整数/资源/错误处理/逻辑/并发/注入），产出 `scan_issues.json`。
纪律约束：只扫 diff 涉及函数及直接上下文；每条问题必须给出可执行的
trigger_condition；**零产出合法且受鼓励**（误报浪费下游整条验证链路）。

**复现用例正向断言约定**：gen（scan 变体 prompt）生成的复现用例断言
"程序行为正确"——PASS=程序正常=疑似误报，FAIL=程序异常=缺陷坐实。

**四态裁决**（确定性规则，无 LLM）：
- `confirmed`：复现用例 FAIL 且失败类型是业务缺陷
- `false_positive`：复现用例 PASS
- `inconclusive`：用例未测到点子上（质量问题/未执行/审查未过）→ 保留人工审查
- `unobservable`：gen 静态论证为运行期不可观测（引用其论证理由）

已知局限：scan-agent 无 base 版本行为对比能力，"上游固有缺陷"与"本次
变更新引入回归"的区分依赖人工（后续增强方向：提供 base 版本函数源码
做对比）。

## 3. 模块清单

```
aicoverage/
├── mrdiff.py            # 本地 git diff 提取（--relative，纯行号，不猜函数名）
├── diffextract.py       # 变更行 → 变更函数（CodeGraph 行区间归因 + resolution）
├── callgraph.py         # CodeGraph CLI 封装 + 反向 BFS + 三种分批策略
├── incremental.py       # 覆盖率 scope 收窄视图 + 增量 delta
├── scanverify.py        # 扫描轨：S1 扫描 → S2 复现用例 → S3 审查 → S4 执行 → S5 裁决
├── mr_loop.py           # MR 主编排：M0-M4 + mr_final_report.md
├── prompts/
│   ├── scan_agent.md    # 本地聚焦扫描 agent（只写 scan_issues.json）
│   └── scan_gen_agent.md# 复现用例生成变体（正向断言，经 prompt_override 注入）
└── loop.py              # run_loop 新增 target_functions/skip_build/target_context
```

## 4. 配置与 CLI

```toml
[codegraph]             # MR 增量闭环用（可选，不开启则 aicov mr 明确报错）
enabled = true
index_dir = ".codegraph"     # `codegraph init` 产物目录（相对 source.path）
entrypoints = ["main"]        # 反向调用链 BFS 的入口锚点（裸函数名）
```

```bash
aicov mr --base origin/main --head HEAD --yes          # 双轨全跑
aicov mr --base ... --skip-scan                        # 只跑覆盖轨
aicov mr --base ... --skip-coverage                    # 只跑扫描轨
aicov mr --base ... --with-kb                          # 闭环前先构建知识库
```

前置条件：项目已建 CodeGraph 索引（`cd <source> && codegraph init`）。
索引缺失时明确报错并给出建索引命令，不静默降级。

## 5. 验证记录

双轨能力经真实项目端到端验证（wrk 压测工具）：

- **纯注释改动**：覆盖轨 scope 内首轮即达标；扫描轨正确产出 0 问题
  （"零产出合法"路径验证）
- **注入缺陷改动**（校验条件对无符号数恒为假的死代码）：scan-agent 精准
  发现（confidence=high，根因含 sscanf 回绕完整链路）→ gen 生成 3 个参数
  化正向断言复现用例 → verify pass → 执行 FAIL → 裁决 confirmed；
  同时覆盖轨增量 func 100%/cond 93.1% 达标

## 6. 后续增强方向

- scan-agent 提供 base 版本函数源码对比（区分上游固有缺陷 vs 新引入回归）
- htmlreport 的 MR 增量高亮（变更文件/函数标记）
- 行级精确增量覆盖率（当前为函数级）
