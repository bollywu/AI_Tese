"""AIcoverage — 面向任意 C/C++ 项目的自动化测试覆盖率闭环。

核心设计取舍（相对"LLM 全程托管"方案）：

- 确定性优先：构建/执行/覆盖率采集/报告拼装全部是纯 Python 代码；
  LLM 只做单点语义决策（生成/审查/归因/扫描/裁决）。
- 本机闭环：gcc --coverage 插桩 → pytest 执行 → gcov 采集，全部本地
  subprocess，不依赖远程设备/容器/外部平台。
- 「原子函数 → 用例搭积木」用例工程学 + gen/verify/quality 多 Agent
  职责分离，执行日志三要素保证可审计。

完整闭环：analyzer(需求/源码解析) → build(插桩构建) → coverage(缺口诊断)
→ gen(用例生成) → verify(静态审查) → execute(本地 pytest，确定性)
→ quality(失败分析) → coverage(增量) → 循环直到 func/cond 达标或早停。
"""

__version__ = "0.1.0"
