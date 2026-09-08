# AIcoverage 多语言后端设计：Go / Java Web 服务覆盖率闭环

> **文档状态**：设计稿（未实现）。目标是把 AIcoverage 从「C/C++ + gcov」扩展到「Go / Java 写的 Web 服务」，闭环语义（需求解析 → 用例生成 → 本地执行 → 覆盖率 → 迭代补测 → 报告）与 C/C++ 完全同构，便于论文做跨语言/跨端对比。
> 关联文档：[README_zh](../README_zh.md)、[PLAN_mr_incremental_closure.md](./PLAN_mr_incremental_closure.md)、[PLAN_cend_image_interaction.md](./PLAN_cend_image_interaction.md)（C 端图像交互，功能轨与覆盖轨拆分的思路与本稿同一抽象层）。
> 一句话目标：**只动"语言触点"，不动状态机**——C/C++ 现有能力零回归，Go/Java 用各自官方覆盖工具链接入同一 `CoverageReport` 契约。

---

## 0. 语言触点分析（为什么引擎本身不用改）

AIcoverage 的状态机（`loop.py`）只消费两类东西：
1. **测试执行结果**：`run_tests()` 产出的 junit/execution.json（语言无关）；
2. **覆盖率快照**：`CoverageReport`（`gcov.py` 的数据模型，序列化为 coverage.json）。

`CoverageReport` 的字段是（函数 file/name/start_line/end_line/execution_count + 分支 line/方向 + 逐行 line_counts + 聚合 func/cond/line），`loop` 的 gap/达标/早停、`incremental`、`finalreport`、`htmlreport` 全都只依赖这个 schema。

被"语言"绑死的实际只有 4 个触点：

| 触点 | C/C++ 现状（代码位置） | Go | Java |
|------|------------------------|----|------|
| ① 源码/函数清单 | `config.source_files()` 只认 `.c/.cc/.cpp/.cxx`（`config.py:233-235`）；函数提取在 `source.py`（ctags/正则） | `.go`；函数清单走 `go/parser` AST（标准库） | `.java`；方法清单来自 jacoco XML（自带类/方法/行） |
| ② 插桩构建与校验 | `build.py` 校验 binary 存在 + **源码树下有 `.gcno`**（`build.py:85`） | `go build -cover`（Go 1.20+，编译期插桩） | JaCoCo **运行期** `-javaagent` 插桩（构建只需产出 class/jar）——语义变化见 §2 |
| ③ 覆盖率解析 | `gcov.py` 跑 `gcov -i -b` → CoverageReport | `go tool covdata merge` → coverprofile 解析 → CoverageReport | `jacococli report --xml` → CoverageReport |
| ④ 每轮清零 + 采集调用点 | `clean_gcda()` / `gcov_collect()`（`executor.py:127,168`、`loop.py:326`、`cli.py` coverage 子命令） | 清空 `GOCOVERDIR`；merge 后解析 | 删/重置 `jacoco.exec`；停服后解析 |
| ⑤ 单测通道（e2e 不可达） | `[unittest]` `compile_unit_driver/run_driver`（宿主 gcc 编译 driver） | 天然 `go test` + 该包覆盖率；或对函数写单测 | JUnit + jacoco(offline) |

> 现状硬编码点集中在：`gcov.py`（`gcov -i` 私有 JSON）、`executor.py` 与 `loop.py` 与 `cli.py` 里直接调用 `clean_gcda/gcov_collect`、`config.py` 的 `language ∈ {c,cpp}` 校验与后缀白名单（`config.py:351`）。

---

## 1. 核心抽象：CoverageBackend 接口

新增 `aicoverage/backends/`，定义一个 backend 只负责 4 件事，并统一产出 `CoverageReport`：

```python
# aicoverage/backends/base.py（契约，示意）
class CoverageBackend:
    language: str                       # "c" | "cpp" | "go" | "java"

    def verify_build(self, cfg) -> list[str]:
        """build 后校验插桩是否生效；返回错误列表（空=通过）。
        c/cpp: 源码树下出现 .gcno（搬现有 build.py 逻辑）
        go   : go>=1.20 可用 + binary 存在 + go tool covdata 可用
        java : 目标 classes/jar 存在 + jacoco agent jar 已配置（运行期插桩，无编译期产物校验）"""

    def clean(self, cfg, work_dir) -> None:
        """本轮清零。c/cpp: 删 .gcda；go: 清空 GOCOVERDIR；java: 删 jacoco.exec"""

    def collect(self, cfg, work_dir) -> "CoverageReport":
        """work_dir 内收集计数文件 → CoverageReport（解析细节见 §2/§3）"""

    def source_suffixes(self) -> tuple[str, ...]:
        """供 config.source_files 使用的后缀白名单"""

    def env(self, cfg, round_dir) -> dict[str, str]:
        """注入 pytest 进程的环境（如 AICOV_COVERDIR / jacoco destfile 路径）"""
```

调用点收敛为一个门面（避免四处 if-language）：

```python
# aicoverage/backends/__init__.py
def get_backend(cfg) -> CoverageBackend: ...     # 按 cfg.language 分发，缺省 gcov（c/cpp）
# executor.run_tests / loop.py 基线 / cli coverage 里的
# clean_gcda(...)+gcov_collect(...)  全部替换为  get_backend(cfg).clean()/.collect()
```

配置改动（全部向后兼容，旧 `aicoverage.toml` 零影响）：

```toml
[project]
language = "go"            # 校验放行 c/cpp/go/java（改 config.py:351 的合法性列表）

[coverage]
backend = ""               # 空 = 按 language 自动推断: c/cpp→gcov, go→go, java→jacoco

# 语言专属参数（仅对应 backend 需要时读取；放各段避免塞爆公共字段）
[go]
coverdir = ".aicoverage/coverdata"   # GOCOVERDIR 基准（每轮在其下建子目录）

[java]
jacoco_agent = "/opt/jacoco/lib/jacocoagent.jar"
exec_dir     = ".aicoverage/jacoco"          # jacoco.exec 落点
include_globs = ["src/main/java/**/*.java"]  # 覆盖口径（过滤框架/生成的类）
exclude_globs = ["**/*Test*", "**/generated/**"]
```

**关键收益**：`CoverageReport` schema 不变 → `loop.py / incremental.py / finalreport.py / htmlreport.py / badcase / docstyle / MR 编排` 全部零改动。

---

## 2. Go Web 服务

### 2.1 工具链与流程

Go 1.20+ 支持把覆盖率插桩编进**可执行二进制**：

```bash
# 插桩构建（build_cmd，项目方提供）
go build -cover -o build/svc .

# 运行：设 GOCOVERDIR；正常关停进程后
go tool covdata merge -o build/covmerged "$GOCOVERDIR"
# 后续由 backends/go.py 解析（等价 gcov 采集）
```

每轮闭环：
```
executor 前置: backend.clean() → 建本轮 GOCOVERDIR
pytest: 用例 session 级 fixture 启动 svc(注入 GOCOVERDIR=本轮目录) → 各用例发 HTTP 请求断言 → 关停 svc(触发落盘)
executor 后置: backend.collect() = go tool covdata merge + coverprofile 解析 → coverage.json
```

### 2.2 coverprofile → CoverageReport 映射（backends/go.py 的解析规则）

coverprofile 是**基本块**记录：`file.go:startLine.startCol,endLine.endCol numStmt count`，无函数名、无分支语义。映射：

| CoverageReport 字段 | Go 的来源 |
|---------------------|-----------|
| 函数清单 + 行区间 | `go/parser` 解析源码 AST 提取每个函数体行区间（复用现 `source.py` 的"清单先行"思路，只是换解析器） |
| 函数是否命中 | 函数体内存在 `count>0` 的行 → hit（execution_count = 函数体命中行数的近似，见说明） |
| 逐行 line_counts | 块行区间→行：被 ≥1 个 `count>0` 的块覆盖的行记 count=1（Go 无"精确执行次数"，二值化即可），全绿/全红与 gcov 同语义 |
| 分支 | **无**（Go 标准覆盖不含 branch）→ `branch_total=0`，走现有 `cond_vacuous` 路径显式标注"Go 无分支覆盖口径" |
| 聚合 | func_hit/func_total、line_hit/line_total 与 gcov 口径一致，可跨语言对比 |

> HTML 报告在 Go 下：函数/行两列照常，条件列显示 vacuous（已有 `cond_vacuous` 表示）；`htmlreport.py` 无需改 schema，只加一个"Go 下隐藏/标注条件列"的小开关（可选）。

### 2.3 单测通道与用例形态

- e2e 不可达函数（N1/N3/N5）：Go 侧"单测 driver" = 给目标包写一个 `_test.go`（`go test` 自带 `-cover`），等价现有 `[unittest]`；harness 加 `run_go_test(pkg, cover_out)` 原子即可。
- E2E 主通道：`session` 级 fixture 起服务 + stdlib `http` 客户端请求（确定性执行阶段继续零第三方依赖）。

### 2.4 aicoverage.toml 示例

```toml
[project]
name = "go-svc"
language = "go"

[source]
path = "."
include_globs = ["cmd/**/*.go", "internal/**/*.go"]

[build]
clean_cmd = "rm -rf build"
build_cmd = "go build -cover -o build/svc ./cmd/svc"
binary = "build/svc"

[coverage]
func_target = 90.0          # Go 建议用 func+line 双口径；cond 列 vacuous
cond_target = 100.0

[test]
dir = "tests"
timeout = 600

[llm]
model = "your-model-name"
max_turns = 120
```

---

## 3. Java Web 服务

### 3.1 工具链与流程（注意：运行期插桩）

JaCoCo 的常用形态是 **javaagent 运行期插桩**，所以"插桩构建"变成"构建产物 + 启动参数"：

```bash
# 构建（build_cmd；无需编译期覆盖插桩）
mvn -q -DskipTests package        # 或 gradle assemble → target/*.jar

# 运行：javaagent 插桩（fixture/conftest 里做）
java -javaagent:/opt/jacoco/lib/jacocoagent.jar=destfile=.aicoverage/jacoco/jacoco.exec,append=false \
     -jar target/svc.jar --server.port=<free_port>
# 用例跑完 → dump/停服落盘 jacoco.exec
# 解析（backends/jacoco.py）
java -jar jacococli.jar report .aicoverage/jacoco/jacoco.exec \
     --classfiles target/classes --sourcefiles src/main/java --xml .aicoverage/jacoco/report.xml
```

`verify_build` 语义相应变化：不校验"编译期插桩标记"，改校验 `target/classes`（或 jar）存在 + jacoco agent jar 路径配置正确。这一点要在文档/README 里对用户讲清（C/C++ 是"编出来就有计数"，Java 是"跑起来才插桩"）。

### 3.2 jacoco XML → CoverageReport 映射（backends/jacoco.py 的解析规则）

`report.xml` 结构：`report/package/class[@sourcefilename]/method[@line] + line[@nr, mi, ci, mb, cb]`。

| CoverageReport 字段 | jacoco 来源 |
|---------------------|-------------|
| 函数清单 + 行区间 | `class(@sourcefilename 去 .java) + method(@name/@line 起止行)`；`include_globs` 对 `.java` 相对路径过滤（含排除第三方/框架类） |
| 方法命中 | 方法指令计数器 `ci>0` → hit；execution_count 二值（jacoco 无执行次数，只有指令 ci/mi） |
| 分支 | 每行 `mb/cb`（missed/covered branch）：branch_total 累加 `mb+cb`，hit=cb → 与 gcov 的"分支方向至少命中一次"口径对齐，**cond 列可直接比 C/C++** |
| 逐行 line_counts | `line@ci`：`ci>0` → 1，`mi>0 && ci==0` → 0（与 Go 一样二值化；jacoco 行级没有执行次数） |
| 聚合 | func/cond/line 三列与 gcov 口径 1:1，**Java 是三种语言里口径最齐的** |

> htmlreport"执行次数"列对 Go/Java 二值化即可（0/1）；如需要真次数，C/C++ gcov 独有，报告里标注清楚，避免论文里被误读。

### 3.3 单测通道与用例形态

- 单测通道：JUnit + jacoco **offline 插桩**（`org.jacoco.ant` 或 maven plugin 的 `jacoco:instrument`）跑 `*Test.java`，等价现有 `compile_unit_driver`；harness 加 `run_java_junit(classpath, agent)` 原子。
- E2E 主通道：embedded 容器（Spring Boot jar / 内嵌 Jetty）由 pytest fixture 启动，端口 `free_port()`；断言走 HTTP 状态码/JSON 体（stdlib `urllib`，维持确定性阶段零第三方依赖）。

### 3.4 aicoverage.toml 示例

```toml
[project]
name = "java-svc"
language = "java"

[source]
path = "."
include_globs = ["src/main/java/**/*.java"]
exclude_globs = ["**/dto/**", "**/config/**", "**/*Application*.java"]  # 圈业务代码，控分母

[build]
clean_cmd = "mvn -q clean"
build_cmd = "mvn -q -DskipTests package"
binary = "target/svc.jar"              # 运行产物

[coverage]
func_target = 90.0
cond_target = 80.0                     # Java 分支口径与 C/C++ 可比

[java]
jacoco_agent = "/opt/jacoco/lib/jacocoagent.jar"
exec_dir = ".aicoverage/jacoco"
```

---

## 4. 执行通道与用例形态（语言无关的公共部分）

无论 Go/Java，被测对象都是"常驻服务进程"，与 C/C++ 的"CLI 一用例一进程"不同，需要新增一组**服务化 harness 原子**（放 `tests/lib/service_atomics.py`，跨语言通用）：

```python
def start_service(cmd_builder, *, port, env_extra=None, log_path=None) -> ServiceHandle: ...
    # 启动服务进程，注入 backend.env()（GOCOVERDIR / jacoco 参数），等待端口就绪（轮询）
def stop_service(h) -> ServiceHandleResult: ...
    # 正常关停（SIGTERM/优雅停机）→ 触发覆盖落盘；失败也必须尝试采集（对齐现有"超时保留覆盖"容错）
def http_get(h, path, headers=None, timeout=10) -> HttpResponse: ...
def http_post(h, path, body, headers=None, timeout=10) -> HttpResponse: ...
def http_json_ok(resp) -> dict: ...                       # 断言 2xx + JSON
def assert_status(resp, code) / assert_json_field(resp, path, expected): ...
def wait_service_ready(h, timeout=30): ...                # 端口/健康检查轮询，不用裸 sleep
```

用例纪律（写进 gen prompt，与 C 端同理）：
- 服务是 `session` 级共享：用例只发请求断言，不做启停（启停交给 fixture 层），避免端口/状态串扰；
- 每轮覆盖率清零发生在 executor 的 `backend.clean()`（轮间），不是用例间；
- 停服失败=本轮覆盖丢失风险：fixture `teardown` 里 stop 失败要报警并把已执行请求保留（覆盖文件在请求期可能已增量写）。

---

## 5. Agent / Prompt 适配（语义为主，骨架不变）

| Agent | 适配点 |
|-------|--------|
| analyzer | 源码清单/入口自动随 `language` 变化；文件预览换 `.go/.java` |
| coverage-agent | N1-N6 不变；N2（协议/对端交互）在 Web 服务下从"少见分支"变**主路径**，提示词里建议"每个未覆盖 handler 优先按 HTTP 触发（N4/N6），服务启动参数/框架初始化类归 N1/N3" |
| gen-agent | 注入 §4 服务原子用法 + HTTP/JSON 断言样板；go 加"函数不可达可写 `_test.go` 走 `run_go_test`"、java 加 JUnit offline 指引（仿 `_unittest_hint` 的注入点做 `_service_hint(cfg)`/`_lang_hint(cfg)`） |
| verify/quality | 静态门禁与失败归因不变；quality 补充"服务启动失败 / 端口被占 / 覆盖未落盘"类 badcase 种子 |
| docstyle | 用例 docstring 门禁（描述/测试点）跨语言复用，不新增字段 |

---

## 6. 里程碑与验收

| 里程碑 | 内容 | 改动面 | 验收 |
|--------|------|--------|------|
| **M0 抽象落地 + C 回归** | 抽出 `backends/` 门面，`executor/loop/cli` 的三处 `clean_gcda+gcov_collect` 调用换成 `get_backend(cfg)`；`config` 放行 go/java 后缀 | backends/base+gcov、config、executor、loop、cli | **C/C++ 现有 wrk/ModSecurity 回归：行为与产物完全一致（git diff 除调用点外为空）** |
| **M1 Go MVP** | `backends/go.py`（`go tool covdata merge` + coverprofile→CoverageReport）+ service 原子 + 一个示例 Go HTTP 服务 target | backends/go、templates、prompts `_lang_hint` | 示例服务闭环 func≥90%（cond 标 vacuous）；`coverage.json` schema 与 C 同结构 |
| **M2 Java MVP** | `backends/jacoco.py`（jacococli XML→CoverageReport）+ 示例 Spring Boot/内嵌容器 target | backends/jacoco、templates | 方法/分支/行与 C 同口径闭环；HTML 条件列可比 |
| M3（后续） | 指标口径文档化（Go 无分支/二值行计数）；与 MR 增量打通（CodeGraph 若不支持 go/java 则 diff 归因回退文件级内置实现）；badcase/wiki 多语言 | incremental、doc 文档 | MR 覆盖轨在 go/java 目标上可用 |

> M0 验收用"回归零行为差异"来担保多语言化不破坏已跑通的 B 端结果——这是论文里"同一工具横向可比"的关键前提。

---

## 7. 风险与规避

| 风险 | 说明与对策 |
|------|-----------|
| R1 Go 无分支覆盖 | 指标口径显式化：Go 用 func+line，cond 走 `cond_vacuous`；论文对比时注明 C/Java 才有分支列 |
| R2 Go `-cover` 版本门槛 | 要求 Go≥1.20；构建校验里加 `go version` 检查；对旧工具链给出"`go test -coverpkg` 集成测试"降级路径 |
| R3 Java 运行期插桩的"构建校验"误导 | build 校验语义变化（产物存在 + agent 配置），README/文档必须写明，否则用户沿用 C 直觉误判"没插桩" |
| R4 服务进程生命周期 | 用例共享服务实例、session 级启停；停服失败仍尝试采集（对齐现有"超时保留覆盖"容错，见 `executor.py` 注释） |
| R5 端口/环境串扰 | `free_port()`（已有）+ fixture 层管启停；服务日志与 jacoco/go cov 文件按轮归档 |
| R6 jacoco XML 行执行次数缺失 | 行计数二值化（0/1）；`htmlreport` 加"次数列对非 gcov backend 显示 hit/miss"开关，避免假次数 |
| R7 Java 类爆炸/框架污染 | `include_globs/exclude_globs` 过滤 + jacoco 排除配置；同 C/C++ 已有 `deps/**` 排除思路 |
| R8 覆盖归因到源码 | Java 需 `--sourcefiles` 对齐路径（否则 XML 无 sourcefilename）；Go 函数清单来自 AST、需与 coverprofile 路径一致（相对 GOPATH/module 前缀归一化） |

---

## 8. 落点顺序建议（git 提交切分）

1. `backends/base.py + gcov backend + 门面` + `config.py`（language 放行、source_suffixes）→ **M0，先跑 C 回归**；
2. `executor.py/loop.py/cli.py` 调用点收敛 → M0 完成；
3. `backends/go.py` + 示例 target + `_lang_hint` → **M1**；
4. `backends/jacoco.py` + 示例 target → **M2**；
5. M3 指标口径文档 + MR/坏例沉淀。

> 原型建议从 Go 起步（`backends/go.py` 解析 coverprofile→CoverageReport 约 200 行可独立验证），跑通后再做 jacoco XML 解析，两者共用 §1 契约，工作量可叠加不返工。
