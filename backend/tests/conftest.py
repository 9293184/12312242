"""pytest 公共夹具：把测试隔离到临时工作区，避免污染真实数据。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.db import initialize_database  # noqa: E402


@pytest.fixture()
def temp_workspace(tmp_path, monkeypatch):
    """把 settings 的运行时目录指向临时目录，并初始化一个空库。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", workspace)
    monkeypatch.setattr(settings, "db_path", workspace / "paperreading.db")
    initialize_database(with_seed=False)
    return workspace
