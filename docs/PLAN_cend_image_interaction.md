# AIcoverage 面向 C 端（用户图像/界面交互）的功能与覆盖测试闭环设计

> **文档状态**：设计稿（未实现）。替换旧稿 `PLAN_cnative_mobile_coverage.md`（原"移动 App native 覆盖率"方向已被本稿取代）。
> 关联文档：[README_zh](../README_zh.md)、[PLAN_mr_incremental_closure.md](./PLAN_mr_incremental_closure.md)。
> 一句话目标：**让 AIcoverage 从"测服务端/CLI 的 C/C++（B 端）"扩展到"测面向用户的图像/界面交互软件（C 端）"——既能对内核代码跑 gcov 覆盖率闭环，又能产出可回归的、以"用户看到的图像/交互结果"为断言的 UI 功能用例**。

---

## 0. 术语与目标形态

| 术语 | 含义 |
|------|------|
| B 端（现状基线） | 服务端 / CLI / 库型 C/C++ 软件（wrk、ModSecurity）。入口是 argv/stdin/网络，输出是文本/退出码/服务行为 |
| C 端（本文目标） | **面向用户的图像/界面交互软件**。入口是用户的可视界面操作（点、拖、绘制、缩放、输入），可观察结果是"用户看到的画面/图像"（渲染结果、画布、地图图层、导出的图片文件等） |
| 功能用例（F 轨） | 黑盒 UI 用例：给定用户操作序列 → 断言**可见/图像化的结果**。目标是回归功能清单 |
| 覆盖用例（C 轨） | 现 AIcoverage 主闭环：需求/缺口 → pytest 用例 → gcov → 迭代补测，目标是函数/分支覆盖率 |

**目标形态决策（一次说清，避免反复）**：C 端要同时满足「图像交互功能测试」和「gcov 覆盖率闭环」，最自洽的目标是 **"主体为 C/C++ 源码 + 有用户可视界面的图像交互软件"**，只差界面层在哪儿：

- **形态 A：桌面 GUI（C/C++ 绘制/图像软件）**——用户交互在窗口上，核心代码即被测代码。gcov 直接可用。
- **形态 B：Web 图像交互页 + C/C++ 内核（WASM 或本地渲染引擎）**——更典型的"用户网页上看图/地图交互"，内核是 C/C++（如地图/渲染引擎），页面是 JS。**内核可 gcov，页面做 UI 功能测试**。
- 形态 C：纯 Web/JS 页面（无 C/C++ 内核）——**只能做 F 轨**；覆盖轨不在 gcov 能力内（如需，JS 覆盖率走 istanbul/c8，属另案，见 §10）。

> 选择标准与候选在 §7。**先决判断**：若论文里 C 端只需要"功能正确 + 用例清单"，选 C；若还要"覆盖率闭环与 B 端同口径对比"（大概率是论文诉求），选 A 或 B，其中 **A 最省事、B 最贴"图像交互"叙事**。

---

## 1. 现状能力 → C 端缺什么

AIcoverage 现有一条完整流水线：`analyzer → build(插桩) → baseline(gcov) → [gap→gen→verify→execute→quality]×N → 报告`。

对 C 端"图像交互功能测试"，缺的不是 agent 编排，而是**入口与断言的可观测对象**变了：

| 环节 | B 端现状 | C 端需求 | 缺口 |
|------|----------|----------|------|
| 被测入口 | CLI 参数/网络输入（`run_binary`） | **界面操作序列**（点击/拖拽/绘制/键盘），或页面交互（浏览器事件） | 需要一个"UI 驱动执行器" |
| 可观测结果 | stdout/退出码/服务状态 | **用户看到的图像**：窗口渲染、画布像素、地图图层、导出 PNG/PDF | 需要一个"图像断言原子库"（图对比/感知哈希/区域采样/图层语义） |
| 用例性质 | 单元/API/协议级（多为白盒，追求覆盖率） | 功能级（黑盒，验收用户价值，天然面向回归清单） | F 轨 gen/verify 提示与产物形态 |
| 报告 | 覆盖率演进/未覆盖函数/疑似缺陷 | + **功能用例回归清单**（每条带前置、操作、可见预期、截图佐证） | 报告多一节 + 证据归档 |

同时覆盖轨（C 轨）在 C/C++ 内核侧**完全复用现引擎**（形态 A 直接测 GUI 进程源码；形态 B 测渲染引擎源码），因此改造量可控、口径与 B 端可比。

---

## 2. 总体方案：双轨（F 功能轨 + C 覆盖轨）一张闭环

```
                             用户视角需求/功能清单（功能点 × 场景）
                                           │
                      ┌────────────────────┴────────────────────┐
            ┌─────────▼─────────┐                     ┌─────────▼─────────┐
            │   F 轨：功能用例    │                     │   C 轨：覆盖闭环（复用）│
            │  func-gen-agent   │                     │  coverage/gen/verify │
            │  需求→UI 操作序列   │                     │  需求/缺口→内核用例    │
            │  图像断言           │                     │  gcov 迭代补测        │
            └─────────┬─────────┘                     └─────────┬─────────┘
                      │ 执行：UI 驱动执行器                       │ 执行：pytest 驱动内核
                      │  （桌面 xdotool/PyAutoGUI/QtTest         │  （现 executor 不变）
                      │   或 Web Playwright，见 §3）              │
                      └──────────────┬──────────────────────────┘
                                     ▼
             确定性执行 → junit/执行日志/截图/导出图产物
                                     ▼
            质量分析（复用 quality-agent）+ 证据归档 + 覆盖率(如可测)
                                     ▼
         loop_final_report.md += 「功能用例回归清单」章节
```

**设计原则（延续既有铁律）**：F 轨里 LLM 只做"功能点理解 + 用例/操作序列设计"和"失败归因"，执行（起应用、找控件、截图、图像断言、产物比对）全部确定性封装为 harness 原子函数；图像断言只给"断言 + expected-vs-observed 打印"，不引入视觉大模型判断，保证可回放、无幻觉。

---

## 3. UI 驱动执行器（新增一层，替代 `run_binary` 的交互入口）

核心不变量：**把"驱动界面"也做成原子函数**，方法论与现 harness 完全一致（先扩原子库，再用例搭积木）。

### 3.1 形态 A（桌面 C/C++ GUI）原子库 `tests/lib/ui_desktop_atomics.py`

- 环境：`Xvfb` 虚拟屏（可并置多目标）+ 被测 GUI 以 `--coverage` 构建；
- 原子函数（首批，之后按需扩）：

```python
def app_launch(binary, args=[], *, xvfb=True, env_extra=None) -> AppHandle: ...
def app_quit(h) -> None                              # 正常退出，触发 .gcda 落盘
def ui_click(h, x, y, button="left"): ...            # 坐标/控件点击
def ui_drag(h, x1, y1, x2, y2): ...                  # 拖拽绘制
def ui_type(h, text): ...                            # 键盘输入
def ui_key(h, name): ...                             # 快捷键
def ui_wait_idle(h, timeout=10): ...                 # 轮询到事件队列空/帧稳定
def screenshot(h, out_png) -> Path: ...              # 抓取窗口截图（X11 抓帧）
def ui_log(h): ...                                   # 应用自身日志（若有）
```

> 驱动方式二选一实现细节（M1 再定）：`PyAutoGUI/xdotool`（最通用、少依赖）或被测应用自带的测试钩子（如 Qt 的 `QTest` 事件注入——更稳但要求目标是 Qt/可注入）。首版建议 xdotool + 截图像素断言，避免绑定框架。

### 3.2 形态 B（Web 图像交互页 + C/C++ 内核）原子库 `tests/lib/web_atomics.py`

- 浏览器驱动用 **Playwright**（无头 Chromium，确定性最强），内核（WASM/本地渲染引擎）独立出可覆盖的 C/C++ 构建；
- 原子函数：

```python
def web_open(url, *, viewport=(1280,800)) -> Page: ...
def web_click(p, x, y) / web_drag(p, x1,y1,x2,y2) / web_type(...) / web_scroll(...) ...
def web_canvas_snapshot(p, selector="canvas") -> Path  # canvas.toDataURL → PNG
def web_export_artifact(p, api_or_download) -> Path    # 拿到导出的图/PDF/数据
def web_wait_idle(p, timeout=10): ...                  # 网络空 + 渲染帧稳定
def assert_canvas_diff(a_png, b_png, region=None, max_pixel_delta=...) : ... # 见 §4
```

### 3.3 关键纪律

- **稳定性优先**：一律"轮询到稳定态"（事件队列空/网络闲/帧不变化），不用裸 `sleep`；
- **会话隔离**：每用例自起自收应用/页面，失败也能截图归档；
- **产物证据**：截图与导出图按 `runs/<run_id>/iter_N/evidence/` 归档，报告链接。

---

## 4. 图像断言方法库（C 端可观测对象的断言原子）

新增 `tests/lib/image_assert.py`，全部是**确定性像素级/结构级断言**（无 LLM、无 VLM），同时打印 expected vs observed：

| 断言原子 | 语义 | 适用 |
|----------|------|------|
| `assert_png_similar(golden, actual, region=None, max_delta=8, max_bad_ratio=0.01)` | 感知哈希 + 区域像素差比例对比，golden 由**确定性录制**产出于基线阶段 | 渲染/绘制结果回归 |
| `assert_canvas_pixel(png, x, y, color_range)` | 关键像素点在指定色域 | 绘图填色、擦除等局部行为 |
| `assert_canvas_has_color(png, color, min_ratio, region=None)` | 某颜色占比下限 | 大面积填充/清除/高亮 |
| `assert_exported_image(export_png, ...)` | 对"导出图片/PDF"复采样校验 + 尺寸/元数据 | 导出功能 |
| `assert_map_feature_count(geojson, count_range)`（B 形态扩展） | 图层语义断言（要素数/属性），由页面导出数据获得 | 地图/GIS 交互 |
| `assert_log_contains(log, needle)`（复用/扩展） | 交互触发的内核日志 | 事件链路 |

**golden 录制**（关键工程决定）：F 轨首轮由 func-gen-agent 生成"操作脚本"，执行器在**空白/已知输入**下录制结果图存为 golden；后续断言比较的是"改动前后用户看到的画面是否变化"——与现有"确定性优先 + 可回放"哲学一致，不依赖外部视觉模型。

---

## 5. 双轨在引擎上的落地（精确改动映射）

> 核心原则：**C 轨一行不改**（现 executor/gcov/报告照跑，只换 target 目录与提示词）；改动集中在 F 轨新增 + 少量编排串联。所有新字段走 `getattr` 容错，旧配置零影响。

| 文件 | 改动 |
|------|------|
| `config.py` | 新增可选 `[profile]`：`kind = "coverage" \| "func" \| "both"`；`[ui]`：`driver = "desktop" \| "web"`、桌面目标 `binary`/`display`（Xvfb）、Web 目标 `url`/`browser`/内核 target 路径；`[image]`：golden 目录、`max_pixel_delta` 等默认断言阈值 |
| `templates.py` | `init --kind func/both` 分发：`ui_desktop_atomics.py`/`web_atomics.py`/`image_assert.py` + 对应 harness 模板；golden 目录骨架；配置模板示例 |
| `executor.py` | 新增 F 轨执行入口 `run_functional_tests()`：与 `run_tests` 同构（junit/执行日志/evidence 归档/超时语义），只是用例体驱动 UI 而非子进程 CLI；复用 `resolve_python`/junit 解析 |
| `loop.py` | 新增轻量编排 `run_func_loop()`（若做成 `both`，先 F 后 C，共享同一 `run_id` 与报告）；C 轨沿用 `run_loop` |
| `agents.py`/`prompts/` | 新增 **func-gen-agent**（prompt：功能点 → 前置/操作序列/可见预期/断言选择）与 **func-verify-agent**（静态审查：操作是否可达、断言是否落在"用户可见结果"上）；新增/复用 quality-agent 做功能失败归因 |
| `docstyle.py` | F 轨用例 docstring 门禁改用 F 字段：`功能点 / 前置条件 / 操作步骤 / 可见预期 / 断言`（保留双录审查性；与现有 C 轨 `描述/测试点` 互不干扰） |
| `finalreport.py` / `htmlreport.py` | 报告新增「功能用例回归清单」节（每条：功能点、severity、操作摘要、可见预期、verdict、证据链接）；HTML 报告加证据浏览（可选 M2） |
| `cli.py` | `aicov loop --kind func|coverage|both`；`aicov func-golden`（录制基线 golden）；`--profile` 透传 |
| `state.py`/`observability.py` | `loop_state.json` 增 `kind`/`func_cases` 摘要；事件流沿用（不需要 schema 破坏） |

**F 轨用例的 verify 闭环**（沿用现有失败→gen 修复回路）：
- 静态：func-verify-agent 审查 + docstyle F 门禁（确定性），fail → func-gen 修复（≤max_verify_retry）；
- 动态：执行失败 → quality-agent 归因 → action_items 回灌下一轮 func-gen（区分「用例写错 / UI 不稳定(flaky) / 真实功能缺陷」三种，与 C 轨失败归因同构）。

---

## 6. 里程碑与验收

| 里程碑 | 内容 | 改动面 | 验收 | 主要风险 |
|--------|------|--------|------|----------|
| **M1 F 轨最小闭环（桌面形态 A 或目标自选）** | 在选定 C/C++ 图像交互软件上跑通"功能清单 → func-gen 出 UI 用例 → 执行器驱动 → 图像断言 → 回归清单" | templates/executor/loop/prompts/docstyle 新增（C 轨不动） | ≥ 10 条功能用例覆盖该软件 3~5 个核心交互功能，能重复跑出稳定 pass/fail | 控件定位与帧稳定（→ xdotool 坐标 + 轮询）；目标选型不合适 |
| **M2 C 轨同口径补上** | 同一目标的内核做 gcov 覆盖率闭环，F/C 报告合一（`both`） | 少量编排（loop/state/finalreport） | 覆盖率达标（或如实报告早停原因），报告含功能清单 + 覆盖率演进 | 目标 GUI 代码与内核耦合（→ 选型时要求内核可分离/可 headless 构建） |
| **M3 形态 B（可选，Web + C/C++ 内核）** | 若论文需要"Web 图像交互"叙事：内核 C 轨 + Playwright F 轨 | web_atomics + executor web 驱动 | Web 页面交互功能用例 + 内核覆盖率同报告 | Playwright 环境/内核 WASM 覆盖路径 |
| M4（后续） | golden 库自管理/失败自动入库；图像断言阈值自适应；与 MR 双轨打通 | — | 见对应设计稿 | — |

---

## 7. 目标选型（重点：还未定）

选型评分权重（论文视角，按序）：
1. **C/C++ 源码可插桩**（否则 C 轨无意义）——一票否决；
2. **有真实的"用户看图像"交互**（渲染/画布/地图/导出图可截、可断言）；
3. **规模可控**（≤几万行、单机可编译运行，AIcoverage 闭环在几小时内能见收敛）；
4. **内核与 UI 可分离**（覆盖轨能在 headless/驱动侧构建，不必每次点亮全 UI）——M2 关键；
5. **论文可比性**：同一工具链、同一报告口径下，与 B 端（wrk/ModSecurity）形成对照。

候选清单与快速判定：

| 候选 | 技术 | 图像交互点 | 可行性 | 建议 |
|------|------|-----------|--------|------|
| **A. 小型 C/C++ 绘图/图像编辑桌面软件**（如 mtpaint（C/GTK）、tuxpaint（C/SDL）、或 Qt 绘制小工具） | C/C++ | 画布绘制、填充、调色、缩放、导出图 | 高（源码可控、X11 下可 gcov、可截图断言） | **M1/M2 默认推荐**：找一个"绘图/图像功能点清晰、行数可控"者 |
| B. 自维护最小 demo（30 分钟可造：Qt/C++ 画布 + 导出 PNG） | C++/Qt | 同上，且 QTest 注入最稳 | 极高 | 兜底；用于先通链路 |
| C. Web 图像交互 + C/C++ 渲染内核（如 mapLibre GL Native 或自建 WASM 绘图引擎） | C++ 内核 + JS 页 | 浏览器画图/地图图层/导出 | 中（需要 Playwright + 内核覆盖路径） | **M3**；论文要"Web C 端"叙事时用 |
| D. 纯 Web/JS 页面 | JS | 页面交互 | 只有 F 轨 | 仅当你放弃覆盖轨可比性时选 |

> 若目标是论文"同一个 AI 测试工具同时覆盖 B/C 两端做对比"，**不要选 D**（无 gcov 口径）。默认建议：M1/M2 用 A 或 B 打通，M3 视时间上 C。

---

## 8. 风险与边界

| 风险/边界 | 说明与对策 |
|-----------|-----------|
| R1 UI 自动化 flaky（控件定位、帧时序） | 坐标+轮询稳定态；失败必截图归档；quality-agent 区分"写错/flaky/真缺陷"；badcase 沉淀 |
| R2 图像断言脆弱（抗锯齿/主题/平台差异） | golden 用同机同构录制；像素容差 + 区域限定；结构断言（要素数/属性）优先于像素断言 |
| R3 覆盖轨与 UI 进程 | GUI 进程需在 Xvfb 下跑并以正常方式退出才落 `.gcda`；或内核 headless 构建走 C 轨（推荐） |
| R4 golden 图的"合理变化"误报 | golden 差异只用于回归信号，不作绝对正确性；正确性断言用像素色域/结构断言 |
| R5 论文叙事扩大（Web/VLM） | M3 只做"Playwright 驱动 + 确定性像素/结构断言"，不引入视觉大模型判断（保持确定性优先哲学）；若要 VLM 视觉断言，单独立案说明其不确定性与成本 |
| R6 纯 JS 页面覆盖率 | 不在 gcov 体系（另案：c8/istanbul + Playwright 覆盖率报告） |
| R7 目标软件功能是否"可 AI 生成用例" | 需求侧由 analyzer/func-gen 从 README/UI 文本/导出结构提炼功能点清单；首版允许人工圈定 3~5 个核心功能点作为 prompt 输入（与 C 轨 requirement 输入一致） |

---

## 9. 建议落地顺序（git 提交切分）

1. 定目标（§7 默认 A/B，先用 demo 通链路再换真实软件）；
2. `examples/cend_ui_demo/`：桌面 GUI demo + F 轨原子 + golden 录制，跑通 M1；
3. 接真实目标，`both` 双轨合一报告（M2）；
4. 按需开 M3（Web 内核）与 M4（golden 自管理）。

---

## 10. 不做/另案

- 纯 JS 页面的代码覆盖率（走 c8/istanbul，与 AIcoverage 报告形态可借鉴但执行器分离）；
- 视觉大模型做"主观美观度"断言（VLM 判定留作人工复核，不参与自动 pass/fail 决定）；
- 移动 App UI 功能测试（若需，参照本稿形态 B/D 的原子化思路 + 上版设备通道的驱动技术，但不属于 gcov 覆盖主线）。
