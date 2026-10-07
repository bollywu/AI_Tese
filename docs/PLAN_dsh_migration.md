# AIcoverage 基于 DeepSeek Harness (dsh) 二次开发改造计划

> 状态：规划中（2026-09-07）
> 结论：技术上可行。确定性阶段（build/coverage/executor/report）零改动；
> 改造集中于 LLM 编排薄层：runner 后端 + 安全策略移植 + 工具契约映射。

## 0. 背景与动机

AIcoverage 的 LLM 阶段当前依赖腾讯 `codebuddy-agent-sdk`（本地 `codebuddy-headless`
二进制 + CodeBuddy 认证服务）。目标：将 Agent 编排/harness 后端切换为 DeepSeek
官方开源框架 **DeepSeek Harness（`dsh`）**，实现二次开发：
- 摆脱对单一认证服务的耦合，统一到 DeepSeek 生态
- 便于后续接入 DeepSeek 系列模型作评测驱动模型
- 保留 AIcoverage 全部确定性闭环（状态机/gcov/HTML 报告等 90% 代码）

## 1. 现状耦合面（改造必须触碰的最小集合）

| 文件 | 职责 | 改造方式 |
|------|------|----------|
| `aicoverage/runner.py` | 唯一 SDK 入口：`run_agent()` → 流式消息 → 归一化 `AgentRunResult` | 抽出 `AgentBackend` 接口，双后端（codebuddy/dsh） |
| `aicoverage/hooks.py` | `PreToolUse` 安全钩子（命令黑名单/gen 禁 pytest/写白名单/只读角色） | 移植到 dsh 工具执行管线/权限插件，**语义等价回归** |
| `aicoverage/agents.py` | 7 角色工具名白名单 + prompt 加载 | 工具名映射到 dsh 工具；prompt 引用同步 |
| `aicoverage/agent_call.py` | 重试/退避/幻觉判定/上下文重启 | 保留（harness 无关） |
| 依赖 `codebuddy_agent_sdk` | 惰性 import（见 runner.py 134、hooks.py 100、agents.py 110） | 改为按 backend 动态 import |

不动：loop.py / mr_loop.py / executor.py / gcov.py / config.py / state.py /
observability.py / htmlreport.py / finalreport.py / kb.py / badcase.py …

## 2. 与 dsh 的关键差距

| AIcoverage 需要 | dsh 现况 | 对策 |
|---|---|---|
| 每次调用的工具调用证据链（tool_uses==0 幻觉铁律） | headless 只输出最终回答，无中间 transcript | **方案 A**：为 dsh 写落盘插件，在 tool 执行管线把事件写 JSONL，Python 侧读文件（契合 AIcoverage 文件契约）；方案 B：用 dsh Python SDK / API gateway |
| token/usage/cost 统计（events.jsonl 与报告依赖） | 未确认结构化暴露 | 插件/telementry 补或允许缺失降级 |
| PreToolUse 前置拦截 + block reason 回流给模型 | approval/permission/sandbox/policy | 移植为 dsh 策略插件，per-agent profile |
| 工具名契约 | dsh 自带 filesystem/shell/subagent… | agents.py 映射表 |
| 模型名 | dsh provider 模型配置 | config model → dsh profile；DeepSeek 模型原生 |

## 3. 分阶段计划

### Phase 0：最小可行性验证（桥接）
- 安装 dsh（npx 运行 + 源码 clone/pnpm，二开准备）
- 用 dsh headless 跑一次分析式任务，确认：① 工具事件可落盘可读；
  ② 能否实现"禁执行 pytest"类拦截；③ token 统计可得性
- 产出：`docs/dsh_phase0_probe.md` 验证结论；判定方案 A/B

### Phase 1：后端抽象（不改变现有行为）
- `runner.py` 抽 `run_agent()` 为 `AgentBackend` 协议；默认 `backend=codebuddy`
- 新增 `backends/dsh_backend.py`：subprocess 调 dsh headless（或 Python SDK）
  + JSONL 事件解析 → `AgentRunResult`
- `config.py` 增加 `[llm] backend = "codebuddy" | "dsh"`；`agents.py` 工具映射表
- 里程碑：dsh 后端可跑通 1 个只读 agent（如 analyze）并产出与 codebuddy 同构结果

### Phase 2：安全策略移植（最高风险，重点回归）
- hooks.py 语义逐条映射到 dsh 插件/权限预设：
  - 危险命令黑名单、gen-agent 禁 pytest/git、verify/scan 只读、kb 只写 wiki
  - gen-agent 写目录白名单（tests/）、路径前缀判断
- 回归矩阵：对每个角色跑"越权操作被拦截"用例
- 里程碑：verify/scan/quality 三个只读 agent 全切 dsh

### Phase 3：铺开写型 agent
- gen-agent / kb-agent 切 dsh，逐批放行写目录
- usage/cost 缺失的降级策略落进 events.jsonl 与报告
- 里程碑：全 7 角色在 dsh 后端跑通一次真实闭环

### Phase 4：对照回归与收口
- wrk / ModSecurity 上同需求跑双后端对照（覆盖率曲线、失败率、token 用量）
- 更新 README/快速开始/`examples/`；补充 dsh 环境安装说明
- 决定默认 backend 与遗留 codebuddy 兼容策略

## 4. 环境安装清单（当前机器缺口）

| 类别 | 项 | 用途 |
|---|---|---|
| 容器/编排 | docker + compose | 评测沙箱/隔离（被测程序、dsh 工具沙箱） |
| Node | pnpm | dsh 源码 monorepo 构建（二开必需） |
| 构建链 | autoconf automake libtool cmake pkg-config universal-ctags | 目标项目构建 + ctags 函数清单 |
| 开发库 | libssl-dev libpcre2-dev libxml2-dev libyajl-dev libcurl4-openssl-dev libmaxminddb-dev | wrk / ModSecurity v3 插桩构建 |
| Python | pytest、codebuddy-agent-sdk、`pip install -e ".[agent,test]"` | AIcoverage 自身运行与回归 |
| dsh | `npx @deepseek-ai/dsh` + `/home/bolly/deepseek-harness` clone + pnpm install | 运行与二开 |

## 5. 风险与回退

- **成熟度**：dsh 0.1.x developer preview，API 可能有 breaking changes → 双后端并存，
  默认 codebuddy 兜底；协议层独立封装，升级只动 backends/
- **安全语义差异**：拦截器 API 不同 → Phase 2 单独里程碑 + 越权回归矩阵
- **docker 环境**：若当前为无特权容器，dockerd 可能无法启动 → 记录并改为
  dsh 文件系统级沙箱/降级提示
- 回退策略：任一 Phase 未过里程碑，保持 `backend=codebuddy` 默认，不阻塞主线

## 6. 相关文档
- 代码扫描结果：本轮 review（见会话记录）
- dsh 官方：https://deepseek-harness.github.io/deepseek-harness/
