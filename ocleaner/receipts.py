# -*- coding: utf-8 -*-
"""清理历史与回执档案域（从 core.py 迁出）。

包含 history.json 读写（双锁防并发覆盖）及 data/receipts/ 下逐次清理的
JSON 回执落盘、检索与防路径穿越读取。对 core 侧路径与锁经 `_core.`
惰性引用，保持与 selftest / unit_test monkeypatch 及 `--data-dir` 兼容。
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import traceback

from . import core as _core


def load_history() -> list:
    data = _core._load_json(_core.HISTORY_PATH, [], use_cache=False)
    return data if isinstance(data, list) else []


def append_history(entry: dict) -> None:
    # 两层锁：_lock 防本进程多线程交错，_cross_process_lock 防双开实例
    # 各自「读-改-写」互相覆盖；独立回执写入也在同锁内完成，避免同秒 TOCTOU。
    with _core._lock, _core._cross_process_lock("history"):
        hist = load_history()
        hist.insert(0, entry)
        _core._atomic_write(_core.HISTORY_PATH, hist[:200])
        try:
            _write_receipt(entry)
        except Exception:
            traceback.print_exc()  # 回执失败不影响清理本身，也不影响历史入账


def _write_receipt(entry: dict) -> None:
    """把一次清理的逐项结果写成独立 JSON 回执，落在数据目录 receipts/ 下。

    文件名取自清理记录自身的时间戳（与 history.json 里的条目一一对应）；
    解析不出时退回当前时间。
    """
    _core.RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(entry.get("time") or "")
    stamp = re.sub(r"[^0-9]", "", stamp)[:15]  # YYYYMMDD-HHMMSS
    if len(stamp) < 14:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S").replace("-", "")[:14]
    path = _core.RECEIPT_DIR / f"cleanup-{stamp}.json"
    n = 0
    while path.exists():  # 同一秒多次清理时加序号，绝不覆盖
        n += 1
        path = _core.RECEIPT_DIR / f"cleanup-{stamp}-{n}.json"
    receipt = {"tool": "O-Cleaner", "app_version": _core.APP_VERSION, **entry}
    _core._atomic_write(path, receipt)


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return default


def list_receipts(q: str = "", drive: str = "", limit: int = 50) -> dict:
    """列出 data/receipts/ 下已落盘的清理回执摘要（按文件名倒序，支持关键词与盘符过滤）。"""
    q0 = str(q or "").strip().lower()
    drv0 = str(drive or "").strip().rstrip(":").upper()
    lim = max(1, min(200, _safe_int(limit, 50) or 50))
    items: list[dict] = []
    if not _core.RECEIPT_DIR.is_dir():
        return {"ok": True, "receipts": [], "total": 0}
    files = sorted(_core.RECEIPT_DIR.glob("cleanup-*.json"), reverse=True)
    for fp in files:
        data = _core._load_json(fp, None, use_cache=False)
        if not isinstance(data, dict):
            continue
        r_drv = str(data.get("drive") or "").upper()
        if drv0 and drv0 not in r_drv.split(","):
            continue
        raw_res = data.get("results")
        res = raw_res if isinstance(raw_res, list) else []
        if q0:
            hay = " ".join([
                fp.name,
                str(data.get("time") or ""),
                str(data.get("trigger") or ""),
                r_drv,
                " ".join(f"{x.get('label', '')} {x.get('path', '')}"
                         for x in res if isinstance(x, dict)),
            ]).lower()
            if q0 not in hay:
                continue
        freed_n = _safe_int(data.get("freed"))
        items.append({
            "name": fp.name,
            "time": str(data.get("time") or ""),
            "drive": r_drv,
            "trigger": str(data.get("trigger") or ""),
            "freed": freed_n,
            "freed_h": str(data.get("freed_h") or _core.human(freed_n)),
            "failed": _safe_int(data.get("failed")),
            "targets": _safe_int(data.get("targets") or len(res)),
        })
        if len(items) >= lim:
            break
    return {"ok": True, "receipts": items, "total": len(items)}


def read_receipt(name: str) -> tuple[dict | None, str]:
    """按文件名读取单份清理回执（严格限制单段 .json 文件名，杜绝路径穿越）。"""
    nm = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.json", nm) or ".." in nm:
        return None, "非法回执文件名"
    try:
        base = _core.RECEIPT_DIR.resolve()
        fp = (_core.RECEIPT_DIR / nm).resolve()
        if fp.parent != base or not fp.is_file():
            return None, "回执文件不存在"
        data = json.loads(fp.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return None, "回执内容格式无效"
        return data, ""
    except (OSError, ValueError) as exc:
        return None, f"读取回执失败：{exc}"
