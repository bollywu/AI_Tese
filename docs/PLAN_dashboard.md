> **文档状态**：设计规划（未实施）

# AIcoverage 前端看板（Dashboard）设计

## 0. 能力概览

看板把 AIcoverage 从「命令行 + 静态报告」升级为「可视化运行观测平台」：

| # | 能力 | 输入 | 输出 |
|---|------|------|------|
| ① | Run 总览：列出/检索历史与进行中的 run | `.aicoverage/runs/*/loop_state.json` | run 列表、状态、exit_reason、耗时 |
| ② | 实时进度：stage 时间线、LLM 调用、成本/token | `events.jsonl`（SSE 增量 tail） | 当前 run 实时视图 + 诊断告警 |
| ③ | 覆盖率趋势：函数/分支/行 pct 逐轮曲线 | `iter_N/coverage.json` | 趋势图 + delta 环比 |
| ④ | 质量与归因：失败分类、flaky、gap 根因 | `quality_report.json` / `gap_items.json` | 归因分布图 |
| ⑤ | MR 增量视图：变更函数覆盖、分批进度、扫描轨 | `mr_summary.json` 等 | MR 详情页 |
| ⑥ | 资产浏览：badcase 库、知识库 wiki、终报嵌入 | `badcases.md` / `wiki/` / `coverage.html/` | 统一入口 |

**数据源零改造**——以上全部产物已存在且结构化（JSON / JSONL / Markdown /
静态 HTML），看板只做「索引 + 推送 + 呈现」，不触碰 run 的写入路径。

## 1. 总体架构

```
┌──────────────────────────────────────────────────┐
│ 前端 SPA（React/Vue + ECharts，构建产物打进 wheel） │
│  Run列表 │ 实时进度 │ 覆盖率趋势 │ 诊断告警         │
│  MR增量  │ Badcase │ 知识库                       │
├──────────────────────────────────────────────────┤
│ 后端 aicoverage/dashboard/                        │
│  server.py   FastAPI 应用 + SSE/WebSocket         │
│  store.py    Run 索引层（复用 cli report 扫描逻辑） │
│  tailer.py   events.jsonl 增量 tail → SSE 推送     │
│  models.py   API 响应模型                          │
├──────────────────────────────────────────────────┤
│ 数据源（零改造，只读）                              │
│  .aicoverage/runs/<id>/loop_state.json            │
│  .aicoverage/runs/<id>/events.jsonl               │
│  .aicoverage/runs/<id>/iter_N/*.json              │
│  .aicoverage/runs/<id>/mr_summary.json 等          │
│  .aicoverage/badcases.md · <source>/wiki/         │
└──────────────────────────────────────────────────┘
```

**核心设计原则**（延续 AIcoverage 铁律）：

- **只读**：看板进程对 `.aicoverage/` 只读，一期不提供任何回写/触发 run 的
  能力；run 的写入仍由闭环编排独占。
- **确定性优先**：所有 API 数据来自磁盘产物的确定性解析，不经 LLM。
- **依赖隔离**：核心保持零硬依赖，看板依赖走 extras：
  `pip install .[dashboard]`（fastapi + uvicorn）。

## 2. 关键设计决策

### 2.1 形态：长驻 Web 服务，而非静态生成

`events.jsonl` 是实时事件流（40+ 事件类型、22 个稳定诊断码），「运行中」
视图必须长驻进程 tail 推送，静态生成方案（类似 `htmlreport.py`）无法表达。
静态方案仅作为无 extras 环境下的降级路径（沿用现有
`python3 -m http.server` 打开 `coverage.html/` 的方式，不属于看板范畴）。

### 2.2 后端索引层：复用 `aicov report` 的扫描模式

`cli.py` 的 `_cmd_report` 已演示「遍历 `runs/` + 读 `loop_state.json`」的
消费模式，`store.py` 将其抽为可复用的 Run 索引层：

- 懒加载：列表接口只读 `loop_state.json` 头部字段；详情接口才解析
  `iter_N/*.json`。
- 缓存：以文件 mtime 为失效键，避免大 run 目录（已有 68KB 级
  coverage.json）重复解析。
- 容错：`loop_state.json` 为整文件重写，读时捕获半写 JSON 异常并重试；
  `events.jsonl` 写入侧带 flock，tailer 只读追加文件天然安全。

### 2.3 实时通道：SSE 优先

`tailer.py` 按字节偏移增量 tail 活动 run 的 `events.jsonl`，解析后经 SSE
推送。SSE（单向、HTTP 友好、断线自动重连）已满足「服务端 → 浏览器」的
全部场景；WebSocket 留待二期出现双向交互（如从看板触发 run）时再引入。

### 2.4 前端工程化：构建产物入库，用户侧免 node

前端源码独立目录（如 `dashboard/ui/`），CI 构建后的静态产物打入 wheel 的
`aicoverage/dashboard/static/`，由 FastAPI 直接托管。一期也可采用更轻方案
（原生 JS + CDN 图表库）与项目「纯静态零依赖」风格对齐，二期升级 SPA。

### 2.5 CLI 与配置接入

- 新增子命令 `aicov dash [--host] [--port] [--workspace-root]`，风格与现有
  10 个子命令一致。
- `aicoverage.toml` 新增 `[dashboard]` 段：`host` / `port` / `refresh_ms` /
  `workspace_root`（支持扫描多个项目的 `.aicoverage/`，如 `port_manage/`、
  `wrk/`）。

### 2.6 Run 类型识别：三类 run，不能只扫 loop_state.json

接口核对发现 `mr_loop.py` 的主 run **从不调用 `init_loop_state`**（只写
`mr_summary.json` + 发事件），若索引层仅以 `loop_state.json` 为键会漏掉全部
MR run。`store.py` 的识别规则：

| 类型 | 判定依据 | 状态/退出原因来源 | 备注 |
|------|---------|------------------|------|
| loop run | `loop_state.json` 存在 | `status` / `exit_reason` | 含 MR 覆盖轨的子批次 run（`MR` 主 run 的 `coverage_batches[].run_id` 指向它们，MR 详情页需关联） |
| MR master run | `mr_summary.json` 存在且无 `loop_state.json` | `mr_summary.status/exit_reason` | run_id 前缀 `MR_`；批次子 run 在详情页聚合展示 |
| analyze-only | 仅 `analysis.md` 存在 | 固定标记 `analyze-only` | 与 `cli.py:_cmd_report` 现有行为一致 |

另：`finished_at` 仅在 `set_exit` 时写入，崩溃/被 kill 的 run 缺该字段——
耗时计算降级为 `now - created_at`（仅对 `status=running` 的 run）。

## 3. 视图与数据源映射

| 视图 | 展示内容 | 数据源 | 产出者 |
|------|---------|--------|--------|
| Run 总览 | run 列表、状态、exit_reason、耗时 | 三类 run 分别识别（见 §2.6）：loop→`runs/*/loop_state.json`；MR master→`mr_summary.json`；analyze-only→`analysis.md` | `state.py` / `mr_loop.py` |
| 实时进度 | 当前 iter/stage、LLM 调用、token | `runs/<id>/events.jsonl`（`task.return` 带 `input/output/total_tokens`，**不带 cost**） | `observability.emit()` |
| 成本视图 | 累计 cost_usd / tokens | `loop_state.json` 顶层 `usage` 字段（`loop.py` 每次 agent 调用后回写）；实时曲线=事件增量 token + 轮询 usage 组合 | `state.py` / `loop.py` |
| 诊断告警 | 22 个诊断码时间线（幻觉/限流/天花板/预算） | events.jsonl diagnostic 事件 | `observability.py` |
| 覆盖率趋势 | 函数/分支/行 pct 逐轮曲线、delta 环比 | `iter_N/coverage.json` | `gcov.CoverageReport.delta()` |
| 缺口根因 | gap 分类分布 | `iter_N/gap_items.json` | coverage-agent |
| 质量归因 | 失败分类、flaky 统计 | `iter_N/quality_report.json` | quality-agent |
| MR 增量 | 变更函数覆盖、分批进度、扫描轨裁决 | `mr_summary.json` / `changed_functions.json` / `scan/bug_verification.json` | `mr_loop.py` |
| Badcase / KB | badcase 库、wiki 索引 | `badcases.md` / `wiki/*.md` | `badcase.py` / `kb.py` |
| 报告入口 | 嵌入现有静态报告 | `coverage.html/` / `loop_final_report.md` | `htmlreport.py` / `finalreport.py` |

## 4. API 草案（P0/P1）

```
GET  /api/projects                     # workspace_root 下的项目列表
GET  /api/runs?project=...             # run 列表（状态/耗时/exit_reason）
GET  /api/runs/{run_id}                # run 详情（状态机 + 逐轮摘要）
GET  /api/runs/{run_id}/coverage       # 逐轮覆盖率序列（趋势图）
GET  /api/runs/{run_id}/quality        # 质量归因聚合
GET  /api/runs/{run_id}/events         # SSE：增量事件流
GET  /api/runs/{run_id}/report         # 重定向/代理 coverage.html/ 与终报
GET  /api/badcases?project=...         # badcase 库
```

## 5. 分期计划

| 期 | 内容 | 验收标准 |
|---|------|---------|
| P0 | 后端索引层 + Run 总览：`store.py`、`/api/runs` 系列、`aicov dash` 命令、`[dashboard]` 配置；前端单页 run 表格 | `pip install .[dashboard]` 后一条命令起服务，列表数据与 `aicov report --list` 一致 |
| P1 | 实时进度：SSE tail、当前 run 详情页（stage 时间线 + 诊断告警 + 成本/token） | run 进行中浏览器秒级看到 stage 推进与诊断事件 |
| P2 | 趋势与分析：覆盖率逐轮曲线、质量归因、gap 根因分布图表 | 历史 run 可回放完整趋势 |
| P3 | MR 视图 + 报告嵌入：MR 增量页、iframe 嵌入 `coverage.html/` 与终报 | MR run 详情与 `mr_final_report.md` 数据一致 |
| P4 | 多项目聚合 + Badcase/KB 浏览（可选）；写能力（看板触发 run）独立评审后另行立项 | 跨 workspace 汇总视图可用 |

## 6. 风险与缓解

1. **依赖红线**：FastAPI / 前端工具链必须隔离在 extras，核心 `pip install .`
   保持零硬依赖——本项目最需要遵守的约束。
2. **前端产物体积**：构建产物打进 wheel 需控制体积（tree-shaking、不内嵌
   sourcemap）；一期轻量方案可完全规避。
3. **并发读**：`loop_state.json` 半写重试、`events.jsonl` 只读 tail，均已有
   写入侧保障（flock / 整文件重写），无需新增锁。
4. **大 run 目录性能**：索引层懒加载 + mtime 缓存；列表接口不做全量
   coverage.json 解析。
5. **权限边界**：只读服务仍可能暴露源码路径等信息，默认绑定 `127.0.0.1`，
   对外暴露由用户显式配置。

## 7. 接口核对记录（2026-10-06，对照后端源码逐项验证）

已验证一致的契约：

- `loop_state.json`：`run_id/status/exit_reason/created_at/finished_at/
  thresholds/limits/iterations[]`（`state.py:init_loop_state`/`set_exit`）
- `events.jsonl`：`ts/type/run_id/pid/host/iter/stage/agent/data`；
  40+ 事件类型、22 个诊断码带 severity（`observability.py`）
- `coverage.json`：`summary.func_pct/cond_pct/line_pct` + hit/total，
  `delta()` 返回 `func_pp/cond_pp/newly_hit`（`gcov.py:CoverageReport`）
- `gap_items.json {items, noise}`、`quality_report.json {verdict, failures,
  action_items, badcase_candidates}`（`loop.py` / `finalreport.py` 消费方式）
- `mr_summary.json {master_run_id, base_ref, head_ref, thresholds,
  coverage_batches[], scan, status, exit_reason}`（`mr_loop.py`）
- 成本/token：`task.return` 事件带 `input/output/total_tokens`；累计
  `cost_usd/total_tokens` 在 `loop_state.usage`（`loop.py` 预算闸门回写）
- 报告嵌入：`coverage.html/` 位于 `runs/<id>/`，路径存于
  `final_metrics.html_report`（`loop.py:_generate_html_report`）
- 配置：`load_config` 按段 `raw.get(name, {})` 解析，新增 `[dashboard]`
  段对存量配置完全兼容
- badcase/wiki：`<workspace>/badcases.md`（`badcase.py:
  project_badcases_path`）、`<source>/wiki/`

核对后修正的设计（见 §2.6 与 §3）：

1. MR 主 run 无 `loop_state.json`，索引层按三类 run 识别（loop / MR master /
   analyze-only），MR 详情页通过 `coverage_batches[].run_id` 关联子批次 run。
2. 事件流不含 cost，成本视图 = SSE 增量 token + 轮询 `loop_state.usage`。
3. `finished_at` 可能缺失（崩溃 run），耗时计算需降级。
