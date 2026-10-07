"""AIcoverage Web 看板（只读）。

设计见 docs/PLAN_dashboard.md：数据源零改造——索引层（store.py）只读扫描
`.aicoverage/runs/`，服务层（server.py，FastAPI）提供 API 与静态页面。
fastapi/uvicorn 为可选依赖（`pip install .[dashboard]`），本包仅在
`aicov dash` 路径被导入，不影响核心零依赖约束。
"""
