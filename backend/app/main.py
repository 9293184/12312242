"""FastAPI application entrypoint for PaperReading Demo V1."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from app.api.v1 import api_router
from app.core.config import settings
from app.db import initialize_database
from app.services.seed_service import seed_initial_library

app = FastAPI(title="PaperReading Demo V1")

# CORS 允许的前端来源:默认本地开发端口,生产环境通过环境变量 CORS_ORIGINS 覆盖
_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
_allowed_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Origin", "X-Requested-With", "Range"],
)
app.include_router(api_router, prefix="/api/v1")

settings.workspace_dir.mkdir(parents=True, exist_ok=True)
# Pre-create workspace subdirectories so the structure is ready on a fresh device
for _sub in ("storage", "debug_logs", "task_logs"):
    (settings.workspace_dir / _sub).mkdir(parents=True, exist_ok=True)
# NOTE: 不要把 workspace_dir 整体挂载为静态目录。
# workspace 下同时存放 api_config.json(含 API 密钥)、paperreading.db(整库)
# 和 debug_logs/(完整 LLM 提示词与论文正文)，整体挂载会导致这些内容无鉴权泄露。
# 论文 PDF 一律通过 /api/v1/papers/{id}/attachments/{type} 接口按需返回。


@app.on_event("startup")
def on_startup() -> None:
    initialize_database(with_seed=False)
    # 首次启动且库内为空时写入内置文献（三篇大模型论文 + 内置 PDF），
    # 并像「刚上传 PDF」一样触发分析。
    seed_initial_library()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ===== 可选：由后端直接托管前端构建产物（SPA）=====
# 若 frontend/dist 目录存在（生产部署、或本地已 npm run build），后端同时托管前端：
#   1. 已注册的 /api/v1/*、/health 路由优先匹配，不受影响；
#   2. 存在的静态文件（/assets/*、/icon.png、/poem.md 等）直接返回；
#   3. 其余 GET 路径（BrowserRouter 深链接，如 /papers/<id> 直接打开/刷新）
#      一律回退到 index.html，由前端路由接管，避免 404。
# 前后端分离开发（vite dev server）时 dist 即使存在也不影响 /api。
# 可用环境变量 FRONTEND_DIST_DIR 显式指定产物目录。
_default_dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST_DIR", str(_default_dist_dir))).expanduser().resolve()

if FRONTEND_DIST.is_dir():
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_catch_all(full_path: str, request: Request) -> Response:
        if request.method != "GET":
            return Response(status_code=405)
        # 防目录穿越：解析后的文件必须仍在 dist 目录内
        candidate = (FRONTEND_DIST / full_path).resolve()
        try:
            candidate.relative_to(FRONTEND_DIST)
        except ValueError:
            return Response(status_code=404)
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # 未知路径 → SPA 入口（深链接由前端 BrowserRouter 处理）
        return FileResponse(FRONTEND_DIST / "index.html")
