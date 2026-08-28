> **文档状态**：审查结论与实施计划（**全部实施完毕**，2026-08-28，224 单测全绿）
>
> **实施情况**：缺陷 1/2/3 已修；计划 1.1-1.3 2.2-2.4 3.1 3.2 4.1-4.3 5.2-5.4
> 6.1-6.4 全部落地。3.2 的 base 对照经 `[coverage] bug_base_compare` 开关启用
> （默认关：每次失败批次多一次完整构建）。1.3 变异自检落地为 CLI 命令
> `aicov mutate`（按需手动调用，见 `mutate.py`）；4.3 落地为 analyzer 后的
> 可达性富化（`_enrich_plan_reachability`，CodeGraph 反向 BFS 附 reachability 字段）。
> 第二轮补充加固（hooks 对齐 / per-run 重试预算 / gcov 增量缓存等）见
> git 历史 commit 0987349。
>
> **审查日期**：2026-08-27 · **审查范围**：AIcoverage 全项目（29 个模块 + 8 份 agent prompt）
>
> **审查目标**：围绕六项质量诉求做整体审查——① 生成用例无假阳性 ② 执行可用性高
> ③ 遇 bug 能正确分析 ④ 按需求识别对应方法 ⑤ 按扫描问题正确设计用例
> ⑥ 用例尽量 e2e、减少单测

# AIcoverage 测试质量加固计划

## 0. 审查结论概览

现有架构在「确定性执行 + LLM 只做单点决策」上是成立的，以下三处设计已被验证有效，应继续沿用其模式：

| 已有优秀设计 | 位置 | 价值 |
|-------------|------|------|
| 用例文档头确定性门禁（EC-07） | `docstyle.py` | 纯 AST、零 LLM token，静态可审查 |
| 扫描轨四态裁决 + 正向断言约定 | `scanverify.py` | 裁决语义与「bug=测试失败」常识一致 |
| badcase 自回归（LLM 提议 / 代码裁决入库） | `badcase.py` | 防重复踩坑，跨轮沉淀 |

但围绕六项诉求，存在 **3 个真实缺陷**（会产出错误结论）与 **8 类能力缺口**。

**核心判断**：目前抗幻觉能力集中在「用例格式」层面（docstring 有没有写），而在
「用例是否真的验证了行为」「声明是否与事实一致」层面缺少确定性校验——这正是假阳性的主要来源。

---

## 1. 真实缺陷（必须修，非优化项）

### 缺陷 1：扫描轨裁决张冠李戴（目标 ⑤，严重）

**现状**：`scanverify.py` 把本轮所有复现用例**合成一次 pytest 执行**，只得到一个全局
`execution.verdict`，随后 `compute_verdicts()` 对**每个 issue 复用这同一个 verdict** 判定：

```311:317:aicoverage/scanverify.py
        elif execution.verdict == "FAIL":
            entry.update({"verdict": VERDICT_CONFIRMED,
                          "evidence": f"复现用例 FAIL（程序表现异常）："
                                      f"failures={execution.failures} errors={execution.errors}"})
        elif execution.verdict == "PASS":
```

**后果**：3 个 issue 中只有 ISSUE-02 的用例 FAIL 时，ISSUE-01/03 **也被判定 confirmed**。
直接违反目标 ⑤，且对外输出错误的「缺陷坐实」结论（假阳性缺陷报告）。

**修法**：
1. `executor._parse_junit()` 扩展为额外返回**逐用例结果** `{nodeid: pass|fail|error|skipped}`，
   写入 `execution.json` 的新字段 `cases`（向后兼容，旧字段不变）
2. `compute_verdicts()` 通过 `dispositions[issue_id].test_function` 反查该 issue 自己的用例结果，
   逐 issue 独立裁决
3. 反查不到对应 nodeid → `inconclusive`（不猜、不复用全局结论）

**影响文件**：`executor.py`、`scanverify.py`

---

### 缺陷 2：全 skip 用例被判 PASS（目标 ①②，严重）

**现状**：`executor.py` 的 verdict 判定只看 `rc`。pytest 在**全部 skip 时 rc=0**：

```329:335:aicoverage/executor.py
    elif rc == 0:
        result.verdict = "PASS"
    elif rc in (3, 4, 5) or result.tests == 0:
```

而脚手架 `conftest.py` 的 `target` fixture 在二进制不存在时正是 `pytest.skip`：

```169:171:aicoverage/templates.py
    if not p.exists():
        pytest.skip(f"被测二进制不存在: {p}（先 aicov build）")
```

**后果**：一个用例都没真跑，闭环却记 `PASS` → 跳过 quality 分析 → 覆盖率 Δ=0 被误归因为
「覆盖率天花板」而早停。**这是最典型的假阳性：用例全绿但什么都没验证。**

**修法**：
1. verdict 增加 skip 判定：`skipped == tests and tests > 0` → `BLOCKED` +
   `failure_kind="all_skipped"` + detail 指明「疑似被测二进制缺失，先 aicov build」
2. skip 率 > 30%（可配）→ 发 `HIGH_SKIP_RATE` 诊断，并**强制进入 quality 分析**
   （现在只有非 PASS 才进）
3. `finalreport.py` 的执行结果章节显式列出 skip 数（避免「绿」的假象）

**影响文件**：`executor.py`、`loop.py`、`observability.py`、`finalreport.py`

---

### 缺陷 3：manifest 声明的覆盖目标从不校验（目标 ①⑥，中等）

**现状**：`loop.py` 只用 `delta["newly_hit"]` 统计新命中函数，**从未把 manifest 声明的
`targets` / `e2e_functions` 与实际覆盖结果比对**。

**后果**：gen-agent 声明「本轮覆盖 `stats_summary`」，即使实际一行未命中，闭环也不会发现，
声明与事实脱节且无人追责。

**修法**：`[f]` 阶段新增确定性校验 `_verify_manifest_claims(manifest, current_full, delta)`：
- 声明覆盖但实际 `execution_count == 0` 的函数 → 记为 `claim_mismatch`
- 写入 `loop_state.json` + 回流 gen 的 action_items（下一轮必须解释或补测）
- 零 LLM 成本的抗幻觉门禁，与 EC-07 同类

**影响文件**：`loop.py`、`state.py`、`finalreport.py`

---

## 2. 按目标的能力缺口与计划

### 目标 ①：消除假阳性（用例真实可用）

**缺口**：`verify_agent.md` 的 V4 只要求「抽查 2~3 处关键断言」，EC-05 仅列举
`assert True` / `assert res is not None` 两种恒真模式。而 harness 的 `assert_stdout_contains`
是**子串匹配**——`assert_stdout_contains(res, "e")` 这类恒真断言完全检测不到
（ModSecurity 闭环已踩过同类坑：正则未转义导致日志恒命中）。

#### 计划 1.1 恒真/弱断言确定性门禁（新增 `assertquality.py`）

纯 AST 扫描，零 token，检测以下模式并产出 `EC-08`（error 级）：

| 检测项 | 判定 |
|--------|------|
| `assert_stdout_contains` / `assert_stderr_contains` 的 needle 长度 < 3 或纯标点/空白 | 恒真高危 |
| `assert_exit_code_ne(res, <非 0 常量>)` | 几乎恒真 |
| `assert_gt(x, -1)` / `assert_gt(len(...), -1)` | 恒真阈值 |
| 用例体内**完全没有** `assert_*` 调用（只打印不断言） | 无断言 |
| `assert_stdout_matches` 的 pattern 含未转义 `[` / `(` 或裸 `.*` 泛匹配 | 正则恒真高危 |

接入方式与 EC-07 完全一致：`loop.py` verify 阶段自动跑，合并进 `verify_report.json`，
走现有 gen 修复回环。

#### 计划 1.2 断言溯源全量核对（prompt + 契约）

- gen 的 manifest 新增 `assertion_evidence`：每个用例的关键断言标注**预期值来自哪个源码位置**（`file:line`）
- `verify_agent.md` 的 V4 从「抽查 2~3 处」改为「逐条核对声明的证据」——有了明确锚点，
  全量核对成本可控

#### 计划 1.3 变异自检（P3，抗假阳性最强手段）

新增 `aicov mutate --iter N`：对本轮新增用例做**反向验证**——临时把被测目标替换为
「故意失效版」（C/C++ 可用 `-DAICOV_MUTATE` 重编译，或替换二进制为 `/bin/true`），重跑新用例。

> **若用例仍 PASS，说明它根本没验证被测行为 = 假阳性** → 判 `EC-09` 并回流。

这是业界公认的假阳性检测法，成本仅一次额外执行。

---

### 目标 ②：执行可用性

#### 计划 2.1 修缺陷 2（skip 门禁）
见 §1 缺陷 2。

#### 计划 2.2 执行前置自检（fail-fast）

`executor.py` 执行前做确定性校验：被测二进制存在且可执行、`.gcno` 存在（C/C++）、
本轮测试文件可 `ast.parse`。任一不满足 → 直接 `BLOCKED` + 明确 detail，
**不浪费一次完整 pytest + LLM quality 分析**。

#### 计划 2.3 用例级超时隔离

现在只有 pytest 整体 `timeout`，单个用例 hang 会拖垮整轮（rc=124 → 全轮 BLOCKED）。
计划：executor 探测 `pytest-timeout` 可用时追加 `--timeout=<per_case>`，
使单例 hang 只 fail 一条；不可用则降级并 warn（不硬依赖第三方包）。

#### 计划 2.4 flaky 确定性复检

现在 flaky 判定完全靠 quality-agent 主观，而 `quality_agent.md` 又要求
「flaky 判定需要明确的不稳定来源」——**缺数据支撑**。

计划：执行阶段对失败用例**自动重跑一次**（`--lf` 仅失败用例），两次结果不一致 →
确定性标记 `flaky=true` 写入 `execution.json`。让 quality-agent 拿到事实而非猜测。

---

### 目标 ③：Bug 正确分析

**缺口**：`quality_agent.md` 要求 `product_suspect` 必须附复现命令与证据链，但
**没有任何机制校验证据真实存在**，也缺少区分「用例错」与「程序错」的确定性信号。

#### 计划 3.1 product_suspect 交叉证据校验（新增 `bugcheck.py`）

对 quality 报的每个 `report_bug`，确定性校验三项：

1. `evidence` 中的 `file:line` 真实存在，且该行确实在被测源码中
2. 该用例确实 FAIL（对照缺陷 1 修复后的**逐用例结果**）
3. 引用行确实包含相关分支/逻辑（证明不是随手引用一行）

任一不满足 → 降级为 `inconclusive` 并要求补证据。**防止臆测 bug 进入最终报告。**

#### 计划 3.2 base 版本对照定位 bug 归属

`mr_loop` 场景已有 base/head git ref 能力。计划：对失败用例自动在 `base_ref` 版本重跑一次——

| base | head | 结论 |
|------|------|------|
| PASS | FAIL | **强证据：本次变更引入的真 bug**（`regression_confirmed`） |
| FAIL | FAIL | 存量问题 / 用例本身错 |

把 bug 归因从 LLM 推测升级为**版本对照事实**。

#### 计划 3.3 失败三分法决策树（prompt）

现有分类表 6 类平铺，缺判定顺序。改为强制顺序：

```
env_blocked / infra（环境）
  → flaky（用 2.4 的确定性重跑数据）
    → case_bug（必须给源码矛盾点 file:line）
      → 剩余才可能 product_suspect
```

降低把用例 bug 误报为产品 bug 的概率。

---

### 目标 ④：按需求识别对应方法

**缺口**：`analyzer_agent.md` 产出 `test_plan.json`（含 `targets[].functions`），但
需求到方法的映射**既不校验也不可追溯**：

- `targets[].functions` 的函数名**没有任何机制确认它真实存在于源码**（analyzer 可能编造）
- `loop.py` 后续只用 `plan_summary` 文本，**需求覆盖率从未被度量**——最终报告没有
  「需求 X 是否被测到」

#### 计划 4.1 test_plan 幽灵函数校验（接现成能力）

analyzer 产出后，用现有 `source.function_inventory()` 逐个校验 `targets[].functions`
是否真实存在（**能力已具备，只是没接上**）。不存在 → 剔除 + 发
`PLAN_GHOST_FUNCTION` 诊断，要求 analyzer 修正。彻底消除「需求映射到不存在的函数」。

#### 计划 4.2 需求 → 函数 → 用例 → 覆盖 全链路追溯

`test_plan.json` 的 `targets[].id`（T-001）贯穿全链：

- gen 的 manifest 新增 `plan_targets: ["T-001"]`
- `finalreport.py` 新增**需求覆盖矩阵**章节：需求项 / 目标函数 / 对应用例 / 是否已覆盖 / 覆盖率

让「需求是否被测到」可量化，而不只有一个笼统覆盖率数字。

#### 计划 4.3 CodeGraph 增强需求映射（P3）

`callgraph.py` 已封装 CodeGraph（当前只用于 MR diff 归因）。计划在 analyzer 阶段复用：
需求关键词 → 候选函数 → **反向调用链 BFS 找到可从 main 触达的入口** →
直接产出 `trigger` 字段的可执行路径。既提升映射准确率，又天然服务目标 ⑥（找 e2e 触达路径）。

---

### 目标 ⑤：按扫描问题正确设计用例

#### 计划 5.1 修缺陷 1（逐 issue 裁决）
见 §1 缺陷 1 —— **本目标首要修复项**。

#### 计划 5.2 复现用例与 issue 强绑定校验

现在靠 prompt 要求「引用 issue_id」，无强制。改为确定性校验：

- `test_bug_<issue_id>.py` 内每个用例 docstring 必须含 `issue_id` 字段（沿用 docstyle 机制，新增 `EC-10`）
- manifest `dispositions[].test_function` 必须与磁盘实际函数名一致
  （`compute_verdicts` 依赖它却从不校验）

#### 计划 5.3 inconclusive 二次尝试

现在 `inconclusive` 直接终结。计划：对原因是「触发条件构造不出」的 issue，
把 scan-agent 的 `trigger_condition` + 调用链上下文回流给 gen 再试一轮
（复用现有 verify 回环模式），降低「待人工」比例。

#### 计划 5.4 扫描轨纳入 e2e 优先

`scan_gen_agent.md` 只有 `e2e` / `unobservable` 两种处置，**缺少「确实需要单测才能复现」的
合法出口**，会逼 agent 把这类 issue 硬塞成 `unobservable`（污染裁决）。
计划新增 `unit_confirm` 处置，走与覆盖率轨相同的人工确认门禁。

---

### 目标 ⑥：最大化 e2e、减少单测

**现状**：E2E-first 治理（`e2e_first` / `unit_confirm_required` / `_confirm_unit_coverage`）
已落地，Go 侧还有 `go_test_scope.py` 静态兜底。

**关键漏洞（C/C++ 与 Go 不对称）**：Go 会自动扫描 `*_test.go` 兜底检测未声明的纯单测
（`_go_unit_tests`），而 **C/C++ 完全依赖 gen-agent 自觉声明**——gen 若调用了
`compile_unit_driver()` 但不写进 `unit_confirm_required`，门禁**完全静默放过**。

#### 计划 6.1 C/C++ 单测自动检测（补齐对称性，本目标最关键）

新增确定性检测：AST 扫描本轮用例文件，凡调用 `compile_unit_driver` / `run_driver` 的用例
→ 自动识别为单测覆盖，与 `unit_confirm_required` 声明比对。
**未声明的自动加入 pending**（与 Go 的 `_go_unit_tests` 完全对等），并发 `UNIT_UNDECLARED` 诊断。

> 「漏声明」从静默通过变成必然被抓。

#### 计划 6.2 单测配额与趋势约束

`[coverage]` 新增 `max_unit_ratio`（默认 0.15）：单测覆盖函数数 / 总新增覆盖函数数
超过阈值 → 发 `UNIT_RATIO_EXCEEDED` 诊断，并在 gen 下一轮 prompt 中强制要求先尝试 e2e 路径。

把「尽量少用单测」从提示词软约束变为**可度量、可拦截的硬指标**。

#### 计划 6.3 e2e 可达性论证（提高单测门槛）

`unit_confirm_required[].evidence` 现在是自由文本。改为结构化三问必答：

1. 尝试过哪些 e2e 输入构造（具体列出）
2. 为什么都不可达（引用源码 `file:line`）
3. 该函数是否有任何调用方能从 main 触达——**用 `callgraph.py` 反向 BFS 确定性核验**

> 若 CodeGraph 证明存在从入口的可达链 → **直接驳回单测申请**，判定「应走 e2e」。

这是最能压低单测比例的一招。

#### 计划 6.4 报告披露覆盖来源构成

`finalreport.py` 已有「待人工确认」章节。新增**覆盖来源构成**：
e2e 覆盖 N 个 / 单测覆盖 M 个（已确认 vs 待确认）/ 单测占比，
并列出每个单测覆盖函数的 e2e 不可达论证。让「e2e 为主」可被一眼审计。

---

## 3. 实施优先级

| 优先级 | 项 | 目标 | 类型 | 工作量 |
|--------|-----|------|------|--------|
| **P0** | 缺陷 1 逐 issue 裁决 | ⑤ | 修 bug | 中 |
| **P0** | 缺陷 2 skip 门禁 | ①② | 修 bug | 小 |
| **P0** | 6.1 C/C++ 单测自动检测 | ⑥ | 补漏洞 | 小 |
| **P0** | 1.1 恒真断言确定性门禁 | ① | 新门禁 | 中 |
| **P1** | 缺陷 3 manifest 声明校验 | ①⑥ | 新门禁 | 小 |
| **P1** | 4.1 test_plan 幽灵函数校验 | ④ | 接现成能力 | 小 |
| **P1** | 2.2 执行前置自检 · 2.4 flaky 复检 | ②③ | 增强 | 中 |
| **P1** | 6.2 单测配额 · 6.3 可达性论证 | ⑥ | 增强 | 中 |
| **P2** | 3.1 bug 证据校验 · 3.2 base 版本对照 | ③ | 增强 | 中 |
| **P2** | 4.2 需求追溯矩阵 · 6.4 来源构成 | ④⑥ | 报告 | 中 |
| **P2** | 5.2 issue 绑定 · 5.3 二次尝试 · 5.4 单测出口 | ⑤ | 增强 | 中 |
| **P3** | 1.3 变异自检 | ① | 新能力 | 大 |
| **P3** | 4.3 CodeGraph 需求映射 | ④ | 新能力 | 大 |

**建议起点**：P0 中的缺陷 1、缺陷 2、6.1——三项均为真实缺陷/漏洞，改动量小、收益最直接。

---

## 4. 贯穿性设计原则（与现有架构一致）

1. **新增校验一律走确定性代码**（AST / junit 解析 / CodeGraph），不新增 LLM 调用——
   延续 `docstyle.py` 零 token 门禁的成功模式
2. **新问题码接入现有 `verify_report.json` 的 problems 数组**（EC-08 / EC-09 / EC-10），
   复用已有 gen 修复回环，不新建流程
3. **新诊断码接入 `observability.py`**（`HIGH_SKIP_RATE` / `UNIT_UNDECLARED` /
   `UNIT_RATIO_EXCEEDED` / `PLAN_GHOST_FUNCTION`），保持 `events.jsonl` 可回放
4. **产物契约向后兼容**：新字段（`cases` / `assertion_evidence` / `plan_targets`）只增不改，
   旧 run 数据仍可解析
5. **每项都配单测**，沿用现有 154 测试基线，不破坏回归

---

## 5. 新增/修改文件索引

| 文件 | 动作 | 对应计划 |
|------|------|---------|
| `aicoverage/assertquality.py` | 新增 | 1.1 |
| `aicoverage/bugcheck.py` | 新增 | 3.1 |
| `aicoverage/executor.py` | 修改 | 缺陷 1、缺陷 2、2.2、2.3、2.4 |
| `aicoverage/scanverify.py` | 修改 | 缺陷 1、5.2、5.3、5.4 |
| `aicoverage/loop.py` | 修改 | 缺陷 2、缺陷 3、1.1、4.1、6.1、6.2、6.3 |
| `aicoverage/config.py` | 修改 | 6.2（`max_unit_ratio`）、2.3 |
| `aicoverage/observability.py` | 修改 | 新诊断码 |
| `aicoverage/finalreport.py` | 修改 | 缺陷 2、缺陷 3、4.2、6.4 |
| `aicoverage/callgraph.py` | 复用 | 4.3、6.3 |
| `aicoverage/prompts/gen_agent.md` | 修改 | 1.2、6.3 |
| `aicoverage/prompts/verify_agent.md` | 修改 | 1.2 |
| `aicoverage/prompts/quality_agent.md` | 修改 | 3.3 |
| `aicoverage/prompts/analyzer_agent.md` | 修改 | 4.1、4.2 |
| `aicoverage/prompts/scan_gen_agent.md` | 修改 | 5.4 |
| `tests/test_assertquality.py` 等 | 新增 | 各项配套单测 |
