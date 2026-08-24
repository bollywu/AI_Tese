"""HTML 覆盖率报告生成器 —— Bullseye covhtml 风格（零第三方依赖，纯标准库）。

对齐经典商业覆盖率工具（BullseyeCoverage covhtml）的报告形态，核心特征：

1. **iframe 三栏布局**：左侧可折叠目录树导航，右侧内容区（可拖动分隔条）
2. **四列指标体系**（每一层级都有）：
   `Function coverage` / `Uncovered functions` / `Condition/decision coverage` / `Uncovered C/D`
3. **层级下钻**：`test.cov`（根）→ 目录 → 文件 → **函数**（每个函数一行，显示其
   自身的函数覆盖与条件/决策覆盖），点击函数名跳到源码页对应锚点
4. **源码页**：函数定义行标 ✔（已覆盖）/ ✘（未覆盖），分支行标 `TF`
   （T=true 分支命中、F=false 分支命中，未命中的方向标红），逐行着色

与 Bullseye 的差异（数据源不同导致的必然差异，已在页面注明）：
- Bullseye 的「条件/决策覆盖」基于其插桩探针；这里用 gcov 的分支（branch）数据等价映射
- Bullseye 用 png 色块，这里用纯 CSS 进度条（避免二进制资源，报告可纯文本 diff）

输出结构：

    <out>/index.html            iframe 框架页（入口）
    <out>/nav.html              左侧目录树导航
    <out>/summary.html          右侧默认内容（根层级汇总）
    <out>/d_<slug>.html         目录层级页（含子目录/文件四列指标）
    <out>/f_<slug>.html         文件层级页（含该文件全部函数的四列指标）
    <out>/s_<slug>.html         源码页（函数锚点 + 逐行着色 + TF 分支标注）
    <out>/style.css
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .gcov import CoverageReport, FileCov

ROOT_LABEL = "coverage"

_STYLE = """\
* { box-sizing: border-box; }
body { margin: 0; padding: 14px 18px; background: #fff; color: #1f2328;
       font: 13px/1.5 -apple-system, "Segoe UI", Roboto, "PingFang SC",
       "Microsoft YaHei", sans-serif; }
a { color: #0b5cad; text-decoration: none; }
a:hover { text-decoration: underline; }
.created { float: right; text-align: right; font-size: 11px; color: #6e7781; }
.crumb { font-size: 13px; margin: 0 0 4px; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 10px 0 14px; }
table.cov { border-collapse: collapse; width: 100%; max-width: 1200px; }
table.cov th { font-size: 11px; font-weight: 600; color: #424a53; text-align: center;
               background: #f6f8fa; border: 1px solid #d0d7de; padding: 5px 8px;
               vertical-align: bottom; line-height: 1.25; }
table.cov th.name { text-align: left; }
table.cov th.selected { background: #eaeef2; }
table.cov td { border: 1px solid #eaeef2; padding: 4px 8px; font-size: 12px;
               white-space: nowrap; }
table.cov td.name { white-space: normal; word-break: break-all; }
table.cov td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.cov tr.first td { background: #f6f8fa; font-weight: 600; }
table.cov tr:hover td { background: #f9fbff; }
.pctwrap { display: inline-flex; align-items: center; gap: 6px; justify-content: flex-end;
           width: 100%; }
.pctnum { min-width: 44px; text-align: right; font-variant-numeric: tabular-nums; }
.gauge { width: 62px; height: 9px; border: 1px solid #b1b8c0; background: #fff; }
.gauge > i { display: block; height: 100%; }
.g-hi { background: #2da44e; } .g-mid { background: #d4a72c; } .g-lo { background: #cf222e; }
.ico { display: inline-block; width: 13px; text-align: center; margin-right: 4px;
       color: #6e7781; font-size: 11px; }
.dim { color: #8c959f; }
/* 源码页 */
.src { font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       white-space: pre; }
.src .row { display: block; }
.src .mark { display: inline-block; width: 30px; text-align: left; user-select: none; }
.src .ln { display: inline-block; width: 54px; text-align: right; color: #8c959f;
           user-select: none; padding-right: 12px; }
.src .cnt { display: inline-block; width: 62px; text-align: right; color: #57606a;
            user-select: none; padding-right: 12px; font-size: 11px; }
.src .hit { background: #e6ffec; }
.src .miss { background: #ffebe9; }
.src .fnhit { color: #1a7f37; font-weight: 700; }
.src .fnmiss { color: #cf222e; font-weight: 700; }
.src .tf { color: #1a7f37; font-weight: 700; }
.src .tfmiss { color: #cf222e; font-weight: 700; }
.src .fnrow { background: #fff8c5; }
.legend { font-size: 11px; color: #57606a; margin: 8px 0 12px; }
.legend b { color: #1f2328; }
/* 导航树 */
body.nav { background: #f6f8fa; padding: 12px 10px; font-size: 12px; }
body.nav ul { list-style: none; margin: 0; padding-left: 14px; }
body.nav > ul { padding-left: 2px; }
body.nav li { margin: 1px 0; }
body.nav .t { color: #6e7781; margin-right: 3px; }
body.nav .pct { color: #8c959f; font-size: 11px; margin-left: 4px; }
body.nav a.sel { font-weight: 600; }
h1.title { font-size: 15px; margin: 0 0 2px; font-weight: 600; }
.note { font-size: 11px; color: #6e7781; margin-top: 16px; max-width: 1000px; }
"""


# ── 指标聚合 ────────────────────────────────────────────────────────

@dataclass
class Metrics:
    """一个层级（根/目录/文件/函数）的四列指标。"""
    func_total: int = 0
    func_hit: int = 0
    branch_total: int = 0
    branch_hit: int = 0

    def add(self, other: "Metrics") -> None:
        self.func_total += other.func_total
        self.func_hit += other.func_hit
        self.branch_total += other.branch_total
        self.branch_hit += other.branch_hit

    @property
    def func_pct(self) -> float | None:
        return (self.func_hit * 100.0 / self.func_total) if self.func_total else None

    @property
    def func_uncovered(self) -> int:
        return self.func_total - self.func_hit

    @property
    def cond_pct(self) -> float | None:
        return (self.branch_hit * 100.0 / self.branch_total) if self.branch_total else None

    @property
    def cond_uncovered(self) -> int:
        return self.branch_total - self.branch_hit


@dataclass
class Node:
    """目录树节点（dir 或 file）。"""
    name: str
    kind: str                     # "dir" | "file"
    rel: str = ""                 # file 节点：相对源码根路径
    children: dict[str, "Node"] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)

    @property
    def page(self) -> str:
        prefix = "f_" if self.kind == "file" else "d_"
        return f"{prefix}{_slug(self.rel or self.name)}.html"


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "_", text)
    return s or "root"


def _file_metrics(fc: FileCov) -> Metrics:
    return Metrics(
        func_total=len(fc.functions),
        func_hit=sum(1 for f in fc.functions.values() if f.hit),
        branch_total=len(fc.branches),
        branch_hit=sum(1 for b in fc.branches if b.hit),
    )


def _build_tree(report: CoverageReport) -> Node:
    """按路径层级构建目录树，自底向上聚合指标。"""
    root = Node(name=ROOT_LABEL, kind="dir", rel="")
    for rel, fc in sorted(report.files.items()):
        parts = rel.split("/")
        cur = root
        for i, part in enumerate(parts):
            is_file = (i == len(parts) - 1)
            child = cur.children.get(part)
            if child is None:
                child = Node(
                    name=part,
                    kind="file" if is_file else "dir",
                    rel="/".join(parts[: i + 1]),
                )
                cur.children[part] = child
            cur = child
        cur.metrics = _file_metrics(fc)

    def agg(node: Node) -> Metrics:
        if node.kind == "file":
            return node.metrics
        total = Metrics()
        for ch in node.children.values():
            total.add(agg(ch))
        node.metrics = total
        return total

    agg(root)
    return root


# ── 渲染基元 ────────────────────────────────────────────────────────

def _gauge_cls(pct: float) -> str:
    return "g-hi" if pct >= 80 else ("g-mid" if pct >= 50 else "g-lo")


def _pct_cell(pct: float | None) -> str:
    """百分比 + CSS 色条（对应 Bullseye 的 w*.png 色块）。"""
    if pct is None:
        return '<td class="num dim">&mdash;</td>'
    return (f'<td class="num"><span class="pctwrap">'
            f'<span class="pctnum">{pct:.0f}%</span>'
            f'<span class="gauge"><i class="{_gauge_cls(pct)}" '
            f'style="width:{min(pct, 100):.1f}%"></i></span></span></td>')


def _int_cell(n: int) -> str:
    cls = "num" if n else "num dim"
    return f'<td class="{cls}">{n}</td>'


def _metric_cells(m: Metrics) -> str:
    return (_pct_cell(m.func_pct) + _int_cell(m.func_uncovered)
            + _pct_cell(m.cond_pct) + _int_cell(m.cond_uncovered))


_TABLE_HEAD = (
    '<table class="cov"><tr>'
    '<th class="name selected">Name</th>'
    '<th>Function<br>coverage</th>'
    '<th>Uncovered<br>functions</th>'
    '<th>Condition/decision<br>coverage</th>'
    '<th>Uncovered<br>conditions/decisions</th>'
    "</tr>"
)


def _page(title: str, body: str, *, body_cls: str = "") -> str:
    cls = f' class="{body_cls}"' if body_cls else ""
    return (f"<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n"
            f"<title>{html.escape(title)}</title>\n"
            f"<link href=\"style.css\" rel=\"stylesheet\" type=\"text/css\">\n"
            f"</head><body{cls}>{body}</body></html>\n")


def _stamp(created: str, project: str, run_id: str) -> str:
    bits = [html.escape(created)]
    if project:
        bits.append(html.escape(project))
    if run_id:
        bits.append(html.escape(run_id))
    return f'<span class="created">{"<br>".join(bits)}<br>AIcoverage · gcov</span>'


def _crumb(node: Node, by_rel: dict[str, Node]) -> str:
    """面包屑：coverage/ dir/ dir/ file.c（对齐 Bullseye 的层级链接）。"""
    links = [f'<a href="d_{_slug("")}.html">{ROOT_LABEL}/</a>']
    if node.rel:
        parts = node.rel.split("/")
        for i, part in enumerate(parts):
            sub = "/".join(parts[: i + 1])
            target = by_rel.get(sub)
            last = (i == len(parts) - 1)
            if last or target is None:
                links.append(html.escape(part) + ("" if last else "/"))
            else:
                links.append(f'<a href="{target.page}">{html.escape(part)}/</a>')
    return '<p class="crumb">' + " ".join(links) + "</p>"


# ── 页面生成 ────────────────────────────────────────────────────────

def generate(
    report: CoverageReport,
    out_dir: Path,
    *,
    source_root: Path,
    project_name: str = "",
    run_id: str = "",
    extra_links: dict[str, str] | None = None,
) -> Path:
    """生成 Bullseye 风格 HTML 报告，返回 index.html（iframe 框架页）路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "style.css").write_text(_STYLE, encoding="utf-8")

    created = report.created_at or datetime.now().isoformat(timespec="minutes")
    created = created.replace("T", " ")[:16]
    stamp = _stamp(created, project_name, run_id)

    root = _build_tree(report)
    by_rel: dict[str, Node] = {}

    def index_nodes(node: Node) -> None:
        by_rel[node.rel] = node
        for ch in node.children.values():
            index_nodes(ch)

    index_nodes(root)

    # 目录层级页 + 文件层级页 + 源码页
    for node in by_rel.values():
        if node.kind == "dir":
            (out_dir / node.page).write_text(
                _dir_page(node, by_rel, stamp, extra_links if node is root else None),
                encoding="utf-8")
        else:
            fc = report.files.get(node.rel)
            if fc is None:
                continue
            (out_dir / node.page).write_text(
                _file_page(node, fc, by_rel, stamp), encoding="utf-8")
            (out_dir / f"s_{_slug(node.rel)}.html").write_text(
                _source_page(node, fc, by_rel, stamp, source_root), encoding="utf-8")

    # 左侧导航 + iframe 框架
    (out_dir / "nav.html").write_text(_nav_page(root), encoding="utf-8")
    title = f"覆盖率报告{f' - {project_name}' if project_name else ''}"
    index_path = out_dir / "index.html"
    index_path.write_text(_frame_page(title, root.page), encoding="utf-8")
    return index_path


def _dir_page(node: Node, by_rel: dict[str, Node], stamp: str,
              extra_links: dict[str, str] | None) -> str:
    rows = [_TABLE_HEAD]
    icon = '<span class="ico">&#128193;</span>'
    rows.append(f'<tr class="first"><td class="name">{icon}{html.escape(node.name)}</td>'
                f"{_metric_cells(node.metrics)}</tr>")
    for child in sorted(node.children.values(),
                        key=lambda c: (c.kind != "dir", c.name)):
        ico = ('<span class="ico">&#128193;</span>' if child.kind == "dir"
               else '<span class="ico">&#128196;</span>')
        rows.append(
            f'<tr><td class="name">{ico}<a href="{child.page}">'
            f"{html.escape(child.name)}</a></td>{_metric_cells(child.metrics)}</tr>")
    rows.append("</table>")

    body = [stamp, _crumb(node, by_rel), "<hr>", "".join(rows)]
    if extra_links:
        body.append('<p class="note">' + " · ".join(
            f'<a href="{html.escape(u)}">{html.escape(n)}</a>'
            for n, u in extra_links.items()) + "</p>")
    body.append(
        '<p class="note">列含义：<b>Function coverage</b> 已执行函数占比；'
        "<b>Uncovered functions</b> 未执行函数数；"
        "<b>Condition/decision coverage</b> 条件/决策覆盖（由 gcov 分支数据映射，"
        "统计至少命中一次的分支方向占比）；<b>Uncovered conditions/decisions</b> "
        "未命中的分支方向数。</p>")
    return _page(node.rel or ROOT_LABEL, "".join(body))


def _file_page(node: Node, fc: FileCov, by_rel: dict[str, Node], stamp: str) -> str:
    """文件层级页：**每个函数一行**，显示该函数自身的覆盖结果（Bullseye 核心视图）。"""
    src_page = f"s_{_slug(node.rel)}.html"
    rows = [_TABLE_HEAD]
    rows.append(f'<tr class="first"><td class="name">'
                f'<span class="ico">&#128196;</span>'
                f'<a href="{src_page}">{html.escape(node.name)}</a></td>'
                f"{_metric_cells(node.metrics)}</tr>")

    # 每个函数的分支归属：按函数名匹配 gcov 的 branches.function
    fn_branches: dict[str, list] = {}
    for b in fc.branches:
        fn_branches.setdefault(b.function, []).append(b)

    for f in sorted(fc.functions.values(), key=lambda x: (x.start_line, x.name)):
        brs = fn_branches.get(f.name, [])
        m = Metrics(
            func_total=1, func_hit=1 if f.hit else 0,
            branch_total=len(brs), branch_hit=sum(1 for b in brs if b.hit),
        )
        mark = ('<span class="ico fnhit">&#10004;</span>' if f.hit
                else '<span class="ico fnmiss">&#10008;</span>')
        rows.append(
            f'<tr><td class="name">{mark}'
            f'<a href="{src_page}#fn_{f.start_line}">{html.escape(f.name)}</a>'
            f'<span class="dim"> &nbsp;line {f.start_line}'
            f'{f" &nbsp;exec {f.execution_count}" if f.hit else ""}</span></td>'
            f"{_metric_cells(m)}</tr>")
    rows.append("</table>")

    legend = ('<p class="legend"><b>&#10004;</b> 函数已执行 &nbsp; '
              '<b>&#10008;</b> 函数未执行 &nbsp; '
              'exec = gcov 记录的函数执行次数 &nbsp; '
              f'点击函数名跳转到 <a href="{src_page}">源码</a> 对应位置</p>')
    body = [stamp, _crumb(node, by_rel), "<hr>", legend, "".join(rows)]
    return _page(node.rel, "".join(body))


def _source_page(node: Node, fc: FileCov, by_rel: dict[str, Node], stamp: str,
                 source_root: Path) -> str:
    """源码页：函数定义行标 ✔/✘ + 分支行标 TF + 逐行着色（对齐 Bullseye 源码视图）。"""
    body = [stamp, _crumb(node, by_rel), "<hr>"]
    body.append(
        '<p class="legend">行首标记：<b class="fnhit">&#10004;</b> 函数已覆盖 · '
        '<b class="fnmiss">&#10008;</b> 函数未覆盖 · '
        '<b class="tf">T</b>/<b class="tf">F</b> 分支方向已命中 · '
        '<b class="tfmiss">T</b>/<b class="tfmiss">F</b> 分支方向未命中；'
        "行底色：绿=已执行，红=未执行，无色=不可执行行；"
        "第二列为该行执行次数。</p>")

    src_path = source_root / node.rel
    if not src_path.is_file():
        body.append(f'<p class="dim">源文件不可读：{html.escape(str(src_path))}</p>')
        return _page(node.rel, "".join(body))
    try:
        text = src_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        body.append('<p class="dim">读取源文件失败</p>')
        return _page(node.rel, "".join(body))

    # 行 → 函数（定义起始行）
    fn_at_line: dict[int, list] = {}
    for f in fc.functions.values():
        fn_at_line.setdefault(f.start_line, []).append(f)
    # 行 → 分支
    br_at_line: dict[int, list] = {}
    for b in fc.branches:
        br_at_line.setdefault(b.line, []).append(b)

    line_counts = fc.line_counts or {}
    out: list[str] = ['<div class="src">']
    for i, raw in enumerate(text.splitlines(), start=1):
        count = line_counts.get(i)
        row_cls = ""
        if count is not None:
            row_cls = "hit" if count > 0 else "miss"

        marks: list[str] = []
        anchor = ""
        for f in fn_at_line.get(i, []):
            anchor = f' id="fn_{f.start_line}"'
            marks.append(f'<span class="{"fnhit" if f.hit else "fnmiss"}">'
                         f'{"&#10004;" if f.hit else "&#10008;"}</span>')
        for b in br_at_line.get(i, []):
            label = "F" if b.fallthrough else "T"
            marks.append(f'<span class="{"tf" if b.hit else "tfmiss"}" '
                         f'title="{label} 分支命中 {b.count} 次">{label}</span>')
        mark_html = "".join(marks[:4])
        if fn_at_line.get(i):
            row_cls = (row_cls + " fnrow").strip()

        cnt_txt = "" if count is None else (str(count) if count > 0 else "0")
        out.append(
            f'<span class="row {row_cls}"{anchor}>'
            f'<span class="mark">{mark_html}</span>'
            f'<span class="ln">{i}</span>'
            f'<span class="cnt">{cnt_txt}</span>'
            f"{html.escape(raw)}</span>")
    out.append("</div>")
    body.append("".join(out))
    return _page(node.rel, "".join(body))


def _nav_page(root: Node) -> str:
    """左侧目录树导航（含每层覆盖率摘要）。"""
    def render(node: Node) -> str:
        items = []
        for child in sorted(node.children.values(),
                            key=lambda c: (c.kind != "dir", c.name)):
            pct = child.metrics.func_pct
            pct_txt = f'<span class="pct">{pct:.0f}%</span>' if pct is not None else ""
            ico = "&#128193;" if child.kind == "dir" else "&#128196;"
            sub = render(child) if child.children else ""
            items.append(f'<li><span class="t">{ico}</span>'
                         f'<a href="{child.page}" target="right">'
                         f"{html.escape(child.name)}</a>{pct_txt}{sub}</li>")
        return f"<ul>{''.join(items)}</ul>" if items else ""

    root_pct = root.metrics.func_pct
    root_txt = f'<span class="pct">{root_pct:.0f}%</span>' if root_pct is not None else ""
    body = (f'<h1 class="title">{ROOT_LABEL}</h1>'
            f'<ul><li><span class="t">&#128193;</span>'
            f'<a href="{root.page}" target="right" class="sel">{ROOT_LABEL}</a>'
            f"{root_txt}{render(root)}</li></ul>")
    return _page("Navigate", body, body_cls="nav")


def _frame_page(title: str, content_page: str) -> str:
    """iframe 框架页（左导航 + 可拖动分隔条 + 右内容），对齐 Bullseye index.html。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN" style="height:100%"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
</head>
<body style="display:flex;height:100%;margin:0">
<iframe src="nav.html" style="border:none;height:100%;width:25%" title="Navigate"></iframe>
<div id="splitter" style="background-color:lightgray;cursor:ew-resize;height:100%;width:2px"></div>
<iframe src="{content_page}" name="right" style="border:none;flex:1 1 0%;height:100%"
        title="Content"></iframe>
<script>
const splitter = document.getElementById('splitter');
const left = splitter.previousElementSibling;
const right = splitter.nextElementSibling;
let mouseX = 0, leftWidth = 0;
const mouseMove = (e) => {{
  const deltaX = e.clientX - mouseX;
  const pct = (leftWidth + deltaX) * 100 / splitter.parentNode.getBoundingClientRect().width;
  left.style.width = pct + '%';
  document.body.style.cursor = 'ew-resize';
  left.style.pointerEvents = 'none';
  right.style.pointerEvents = 'none';
}};
const mouseUp = () => {{
  left.style.pointerEvents = 'auto';
  right.style.pointerEvents = 'auto';
  document.body.style.cursor = 'auto';
  document.removeEventListener('mousemove', mouseMove);
  document.removeEventListener('mouseup', mouseUp);
}};
splitter.addEventListener('mousedown', (e) => {{
  mouseX = e.clientX;
  leftWidth = left.getBoundingClientRect().width;
  document.addEventListener('mousemove', mouseMove);
  document.addEventListener('mouseup', mouseUp);
}});
</script>
</body></html>
"""
