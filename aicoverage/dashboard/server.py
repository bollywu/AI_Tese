"""看板 HTTP 服务（FastAPI，只读）。

fastapi/uvicorn 为可选依赖（`pip install .[dashboard]`），故在 create_app()
内惰性导入——核心包保持零硬依赖。一期只读：不提供任何回写/触发 run 的端点。
"""
from __future__ import annotations

from pathlib import Path

from ..config import ProjectConfig
from . import store

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: ProjectConfig):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse
    except ImportError as e:  # pragma: no cover - 依赖缺失在 cli 层已拦截
        raise RuntimeError(
            "看板依赖未安装，请执行: pip install 'aicoverage[dashboard]'"
        ) from e

    app = FastAPI(title="AIcoverage Dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/project")
    def project() -> dict:
        return {
            "name": cfg.display_name,
            "source": str(cfg.source_path),
            "runs_dir": str(cfg.runs_dir),
        }

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return store.scan_runs(cfg.runs_dir)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        detail = store.get_run(cfg.runs_dir, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        return detail

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    return app
