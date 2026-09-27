"""按论文粒度的任务锁。

同一篇论文可能被多条路径同时触发后台分析（重复上传原件、手动重新分析、
批量导入），彼此会争抢 paper_texts / paper_analysis 的写入与 papers.status，
导致状态被旧任务覆盖。这里提供一个非阻塞的按论文锁，保证同一时刻只有
一个分析任务在跑。
"""

from __future__ import annotations

import threading

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(paper_id: str) -> threading.Lock:
    with _guard:
        lock = _locks.get(paper_id)
        if lock is None:
            lock = threading.Lock()
            _locks[paper_id] = lock
        return lock


def try_acquire(paper_id: str) -> bool:
    """非阻塞地获取该论文的分析锁；已被占用则返回 False。"""
    return _lock_for(paper_id).acquire(blocking=False)


def release(paper_id: str) -> None:
    lock = _locks.get(paper_id)
    if lock is not None and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass
