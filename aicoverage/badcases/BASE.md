# AIcoverage 基础设施 Badcase 知识库（工具级，随 AIcoverage 分发）

> **用途**：gen/quality/verify agent 在任务前应参照本库速查索引，检查当前工作是否踩到已知陷阱。
> 本库覆盖**跨项目通用**的工具级坑（SDK/覆盖率采集/模板/断言语义）；
> 项目特定坑沉淀在各项目 `<source>/.aicoverage/badcases.md`（自动累积）。
> 编号前缀 AICB；入库均来自真实事故复盘，非推测。

## 速查索引

| 编号 | 标题 | 类别 | 影响范围 | 日期 |
|------|------|------|---------|------|
| AICB-001 | SDK hooks 必须传 HookMatcher dataclass，裸 dict 报错 | sdk-config | 所有 agent 调用 | 2026-08-21 |
| AICB-002 | fnmatch 的 `*` 会跨 `/`，`src/**/*.c` 匹配不到 `src/foo.c` | glob | 覆盖率过滤/源码枚举 | 2026-08-21 |
| AICB-003 | gcov collect 同名 .gcno 输出互相覆盖（libtool 双重编译） | coverage | libtool 项目覆盖率 | 2026-08-24 |
| AICB-004 | 未补零整数子目录按字符串排序："122" < "56" | coverage | 大项目覆盖率 | 2026-08-24 |
| AICB-005 | CoverageReport 序列化丢 branches/line_counts → 跨轮指标失真 | coverage | HTML 报告/delta | 2026-08-21 |
| AICB-006 | 模板字符串嵌套三引号提前截断生成代码 | template | harness 生成 | 2026-08-24 |
| AICB-007 | timeout=0 语义是瞬间 kill 而非无限等待 | executor | 用例超时设置 | 2026-08-21 |
| AICB-008 | 无分支函数 cond_pct=0 与达标状态自相矛盾（vacuous） | coverage | scope 达标判断 | 2026-08-24 |
| AICB-009 | 用例断言值臆测未读源码 → 批量 case_bug | gen-quality | 用例生成 | 2026-08-24 |
| AICB-010 | 死代码/不可达函数强行伪造用例凑覆盖 | gen-quality | 覆盖率可信度 | 2026-08-24 |

---

## AICB-001: SDK hooks 必须传 HookMatcher dataclass

- **类别**: sdk-config
- **症状**: `run_agent` 传 `hooks={"PreToolUse": [{"matcher": ..., "hook": ...}]}`（裸 dict）报 `'dict' object has no attribute 'hooks'`
- **根因**: CodeBuddy Agent SDK 的 hooks 参数要求 `HookMatcher` dataclass 实例列表，不做 dict 自动转换
- **修复/预防**: `from codebuddy_agent_sdk import HookMatcher`，用 `HookMatcher(matcher=..., hooks=[fn])`

## AICB-002: fnmatch 的 `*` 会跨 `/`

- **类别**: glob
- **症状**: include_globs 配了 `src/**/*.c` 但 `src/wrk.c` 匹配不到，覆盖率 0/0
- **根因**: `fnmatch` 不区分路径分隔符，`*` 能跨 `/`，`**` 语义与 gitignore 不同
- **修复/预防**: glob 语义一律走 `globutil.glob_matches`（gitignore 语义 `**`），禁止裸 fnmatch

## AICB-003: gcov collect 同名 .gcno 输出互相覆盖

- **类别**: coverage
- **症状**: libtool 项目（静态 + PIC 共享库双编译）覆盖率读成 0%，但手动 `gcov -i` 单跑有真实数据
- **根因**: 同一源文件产生两份同 basename 的 .gcno（`src/x.gcno` 与 `src/.libs/x.gcno`），全部 gcov 输出堆同一目录时同名 JSON 互相覆盖，未执行的（无 .gcda）那份可能后处理
- **修复/预防**: 每 gcno 独立子目录输出；合并时按 (file,line) 取 count 更大者（见 AICB-004）

## AICB-004: 未补零整数子目录按字符串排序

- **类别**: coverage
- **症状**: 422 个编译单元的项目，25 个目标函数真实执行了但 coverage.json 全 0
- **根因**: 子目录名 `"0","1",...,"122"` 按**字符串**排序决定处理顺序，`"122" < "56"`——零数据先占位
- **修复/预防**: 合并策略必须**顺序无关**（取 count 最大者），任何依赖 sorted() 顺序的"先到先得"都危险

## AICB-005: CoverageReport 序列化丢 branches/line_counts

- **类别**: coverage
- **症状**: round-trip 后 cond_pct 归零、HTML 逐行着色失效
- **根因**: `save()` 没写 branches/line_counts 字段，`load()` 也没还原
- **修复/预防**: 序列化改动后必须做完整 round-trip 断言（save→load→全字段相等）

## AICB-006: 模板字符串嵌套三引号提前截断

- **类别**: template
- **症状**: 生成的 harness.py 首行语法错误/模块 docstring 被截断
- **根因**: 模板（三引号包裹）内嵌含 `"""` 的示例代码，内层引号提前闭合外层
- **修复/预防**: 模板内嵌示例避免与外层相同定界符；生成后必须 `ast.parse()` 校验

## AICB-007: timeout=0 语义是瞬间 kill

- **类别**: executor
- **症状**: pytest/子进程"秒失败"，日志无内容
- **根因**: 0 不是"无限等待"而是立即超时
- **修复/预防**: 超时必须为正数；配置层校验 `test_timeout > 0`（config.validate）

## AICB-008: 无分支函数 cond_pct=0 与达标矛盾

- **类别**: coverage
- **症状**: scope 内函数 100% 执行、无可测分支，但达标判断 cond<85% 永不通过 → gen 空转
- **根因**: `branch_total==0` 时 `cond_pct` 显示口径为 0.0，与"无可测分支=视为满足"的语义冲突
- **修复/预防**: vacuous cond 处理——无可测分支时视为满足并显式标注 `cond_vacuous`，报告显示口径记 100%

## AICB-009: 用例断言值臆测未读源码

- **类别**: gen-quality
- **症状**: 生成的用例批量 FAIL，evidence 是"源码行为 vs 用例预期"矛盾
- **根因**: gen 未 Read 目标函数源码，凭函数名/常识猜断言值
- **修复/预防**: 断言预期值必须来自源码真实逻辑（gen 铁律第 8 条）；verify 抽查断言与源码一致性

## AICB-010: 死代码强行伪造用例凑覆盖

- **类别**: gen-quality
- **症状**: 为不可达函数编造"调用式"用例，覆盖率虚高但无业务价值
- **根因**: 追求 100% 数字而放弃"用例=真实业务路径"原则
- **修复/预防**: 入口不可达的函数在 manifest 标 verdict_unreachable/verdict_noop 给证据链，拒绝伪造（coverage-agent N5 分类 + MR 闭环 unreachable 批次提示）
