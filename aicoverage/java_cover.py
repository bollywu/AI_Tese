"""Java coverage backend: parses JaCoCo XML reports (method/line/branch level).

Java's coverage is agent-instrumented at test time (JaCoCo javaagent, configured
in the build file): `mvn test` / `gradle test jacocoTestReport` produces
jacoco.xml. The XML structure relevant here:

    <report>
      <package name="com/example">
        <sourcefile name="App.java">
          <line nr="10" ci="2" mi="0" cb="1" mb="0"/>
          <method name="main" desc="(Ljava/lang/String;)V" line="10">
            <counter type="INSTRUCTION" covered="10" missed="0"/>
            <counter type="BRANCH" covered="1" missed="1"/>
            <counter type="LINE" covered="3" missed="0"/>
          </method>
        </sourcefile>
      </package>
    </report>

This backend turns those records into the language-neutral CoverageReport model:
  - function coverage: <method> with INSTRUCTION counter covered > 0 is hit
    (execution_count approximated as covered instruction count)
  - line coverage: <line> ci/mi (covered/missed instructions on that line)
  - branch coverage: <line> cb/mb (covered/missed branches on that line --
    BRANCH counters on methods aggregate all its lines, so line-level cb/mb is
    the finer source and is what we use)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .gcov import BranchCov, CoverageReport, FileCov, FunctionCov


@dataclass
class JacocoMethod:
    name: str
    line: int
    covered_instr: int
    # <method>'s own LINE counter gives the method body span implicitly via the
    # enclosing sourcefile's line records; we keep just the start line.


@dataclass
class JacocoFile:
    """Parsed records for one <sourcefile> (jacoco records paths split between
    package@name and sourcefile@name; `path` holds the joined posix path)."""
    path: str                                              # e.g. com/example/App.java
    methods: list[JacocoMethod] = field(default_factory=list)
    # line nr -> (covered_instr, missed_instr, covered_br, missed_br)
    lines: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)


def parse_jacoco_xml(path: Path | str) -> list[JacocoFile]:
    """Parse a jacoco.xml into per-sourcefile records (missing/corrupt -> [])."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    files: list[JacocoFile] = []
    for pkg in root.iter("package"):
        pkg_name = pkg.get("name", "")
        for sf in pkg.iter("sourcefile"):
            sf_name = sf.get("name", "")
            jf = JacocoFile(path=f"{pkg_name}/{sf_name}" if pkg_name else sf_name)
            for m in sf.iter("method"):
                m_name = m.get("name", "")
                try:
                    m_line = int(m.get("line", "0"))
                except ValueError:
                    m_line = 0
                covered = 0
                for counter in m.iter("counter"):
                    if counter.get("type") == "INSTRUCTION":
                        try:
                            covered = int(counter.get("covered", "0"))
                        except ValueError:
                            covered = 0
                        break
                if m_name:
                    jf.methods.append(JacocoMethod(m_name, m_line, covered))
            for ln in sf.iter("line"):
                try:
                    nr = int(ln.get("nr", "0"))
                    ci = int(ln.get("ci", "0"))
                    mi = int(ln.get("mi", "0"))
                    cb = int(ln.get("cb", "0"))
                    mb = int(ln.get("mb", "0"))
                except ValueError:
                    continue
                jf.lines[nr] = (ci, mi, cb, mb)
            files.append(jf)
    return files


def collect_java(
    source_root: Path,
    jacoco_xml: Path | str,
    *,
    include_filter=None,
    exclude_filter=None,
) -> CoverageReport:
    """Build a CoverageReport from a jacoco.xml report.

    - function coverage: method hit iff covered instructions > 0
    - line coverage: line hit iff covered instructions > 0
    - branch coverage: one BranchCov per covered/missed branch on a line
    """
    from .globutil import glob_matches

    report = CoverageReport(created_at=datetime.now().isoformat(timespec="seconds"))
    for jf in parse_jacoco_xml(jacoco_xml):
        rel = jf.path
        if include_filter and not glob_matches(rel, include_filter):
            continue
        if exclude_filter and glob_matches(rel, exclude_filter):
            continue

        fc = FileCov(file=rel)
        # line is "hit" when any of its instructions executed
        fc.line_counts = {nr: ci for nr, (ci, _, _, _) in jf.lines.items()}
        fc.lines_total = len(jf.lines)
        fc.lines_hit = sum(1 for nr, (ci, _, _, _) in jf.lines.items() if ci > 0)

        # end_line approximation: next method's start - 1, else last known line
        last_line = max(jf.lines) if jf.lines else 0
        sorted_methods = sorted(jf.methods, key=lambda m: m.line)
        for idx, m in enumerate(sorted_methods):
            end_line = (sorted_methods[idx + 1].line - 1
                        if idx + 1 < len(sorted_methods)
                        else max(m.line, last_line))
            fc.functions[m.name] = FunctionCov(
                file=rel, name=m.name,
                start_line=m.line, end_line=end_line,
                execution_count=m.covered_instr,
                blocks=0, blocks_executed=0,
            )

        for nr, (_, _, cb, mb) in jf.lines.items():
            fn_name = ""
            for name, fcov in fc.functions.items():
                if fcov.start_line <= nr <= fcov.end_line:
                    fn_name = name
                    break
            for i in range(cb):
                fc.branches.append(BranchCov(file=rel, line=nr, function=fn_name,
                                             count=1, fallthrough=False, throw=False))
            for i in range(mb):
                fc.branches.append(BranchCov(file=rel, line=nr, function=fn_name,
                                             count=0, fallthrough=False, throw=False))

        report.files[rel] = fc
    return report
