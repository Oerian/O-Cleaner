# -*- coding: utf-8 -*-
"""定时清理调度域（从 core.py 迁出）。

包含预设解析、周期钳位、定时清理执行循环与单目标错误回执构造。
对 core 侧路径常量与状态读写经 `_core.` 惰性引用，保持与
unit_test / selftest 的 monkeypatch 及 `--data-dir` 重定向兼容。
"""
from __future__ import annotations

from datetime import datetime
import time
import traceback

from . import core as _core


DEFAULT_SCHEDULE = {
    "enabled": False,
    "interval_hours": 24,
    "preset": "safe",
    "drive": "C",
    "last_run": 0,
    "next_run": 0,
}


def schedule_state() -> dict:
    s = dict(DEFAULT_SCHEDULE)
    s.update(_core._load_json(_core.SCHEDULE_PATH, {}, use_cache=False) or {})
    # 定时任务执行摘要（#37）：从清理历史里找最近一次定时条目，给启动
    # 横幅用 —— 后台跑完用户无感知，下次打开界面要能看到「它干了什么」。
    try:
        for h in _core.load_history():
            if not str(h.get("trigger") or "").startswith("定时"):
                continue
            s["last_summary"] = {
                "time": h.get("time", ""),
                "freed": int(h.get("freed") or 0),
                "freed_h": _core.human(int(h.get("freed") or 0)),
                "failed": int(h.get("failed") or 0),
                "targets": int(h.get("targets") or 0),
                "drive": h.get("drive", ""),
            }
            break                     # history 是新的在前，第一条即最近一次
    except Exception:
        pass                          # 摘要失败不影响调度状态本身
    return s


def _resolve_preset(targets: list[dict], preset: dict) -> list[str]:
    """把预设解析成目标 id 集合。盘符专属项用带盘符后缀的完整 id 匹配。"""
    sel = preset.get("select", {}) or {}
    base_to_full: dict[str, list[str]] = {}
    for t in targets:
        base_to_full.setdefault(t.get("base_id", t["id"]), []).append(t["id"])

    if "ids" in sel:
        # 显式按 id 指定：完全尊重配置，不做回收站过滤
        # （否则「只清回收站」这类预设会被自己的过滤器清空）
        out: list[str] = []
        for b in sel["ids"]:
            out.extend(base_to_full.get(b, []))
        return out

    risks = set(sel.get("risk", []))
    allow_admin = bool(sel.get("allow_admin", False))
    return [t["id"] for t in targets
            if t.get("risk") in risks and (allow_admin or not t.get("needs_admin"))]


def _schedule_run_interval(s: dict) -> float:
    """把 interval_hours 归一成合法小时数。

    配置可能是手改的、被旧版本写坏的、或前端 bug 传进来的字符串。
    这里一律钳到 [0.25, 24*30]：非法值回落到默认 24 小时，绝不抛异常 ——
    抛异常会让 next_run 不推进，守护线程 30 秒后又来跑一次（见 _run_scheduled）。
    """
    try:
        hrs = float(s.get("interval_hours"))
    except (TypeError, ValueError):
        return float(DEFAULT_SCHEDULE["interval_hours"])
    if not (hrs > 0) or hrs != hrs or hrs == float("inf"):   # 含 NaN / inf
        return float(DEFAULT_SCHEDULE["interval_hours"])
    return max(0.25, min(hrs, 24 * 30))


def _apply_preset_to_targets(targets: list[dict], preset_id: str) -> list[dict]:
    cfg = _core.load_targets()
    preset = next((p for p in cfg.get("presets", []) if p["id"] == preset_id), None)
    if not preset:
        # 未知预设名（被资源包移除 / 手改配置）**不能**回落成「所有不需要管理员的
        # 目标」—— 那会比用户预期宽得多（实测 safe=9 项，回落=22 项），
        # 而定时任务会照这个范围真删。返回空集，由调用方如实报告。
        return []
    ids = set(_resolve_preset(targets, preset))
    return [t for t in targets if t["id"] in ids]


def _scheduler_loop() -> None:
    while True:
        time.sleep(30)
        try:
            s = schedule_state()
            if s.get("enabled"):
                nxt = float(s.get("next_run") or 0)
                if nxt and time.time() >= nxt:
                    # _run_scheduled 内部会锁内重读配置并推进 next_run，
                    # 所以这里传进去的 s 只供它读取 drive/preset 初值。
                    _run_scheduled(s)
        except Exception:
            # 兜底日志：即便 _run_scheduled 自身抛异常（它已有 finally 推进
            # next_run），也不能让守护线程静默空转。
            traceback.print_exc()


def _run_scheduled(s: dict) -> None:
    drive = (s.get("drive") or "C").rstrip(":").upper()
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "drive": drive,
             "trigger": f"定时自动清理（{drive}:）",
             "results": [], "freed": 0, "failed": 0, "targets": 0}
    try:
        targets = _core.build_targets(drive)
        selected = _apply_preset_to_targets(targets, s.get("preset") or "safe")
        if not selected:
            # 预设找不到 / 展开为空：不清理，只记一条可读的原因。
            # 以前这里会静默跑空并推进 next_run，用户永远不知道定时任务没生效。
            entry["results"].append({
                "id": "-", "label": f"预设「{s.get('preset')}」不存在或没有匹配项",
                "path": "", "drive": drive, "freed": 0, "deleted": 0,
                "failed": 0, "skipped": [], "before": 0, "after": 0,
                "seconds": 0.0, "renews": None,
                "error": "本次未执行任何清理（预设无效），请检查定时清理设置",
            })
        entry["targets"] = len(selected)
        for t in selected:
            try:
                r = _core.clean_target(t, drive=drive)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                r = _error_result(t, drive, exc)
            entry["results"].append(r)
            entry["freed"] += r["freed"]
            entry["failed"] += r["failed"]
    finally:
        # 无论本轮成功、失败还是抛异常，next_run 都必须推进 ——
        # 否则守护线程 30 秒后会认为「又到期了」并再删一次。历史上
        # interval_hours 被写成非法值时，这里抛 ValueError 导致 next_run
        # 停在过去，实测在 2 分钟内连续触发了 6 次真实清理。
        entry["freed_h"] = _core.human(entry["freed"])
        _core.append_history(entry)
        with _core._lock:
            cur = schedule_state()          # 锁内重读，别用过期的快照
            now = time.time()
            hrs = _schedule_run_interval(cur)
            cur["interval_hours"] = hrs     # 顺手把非法值就地修正
            cur["last_run"] = now
            cur["next_run"] = now + hrs * 3600
            _core._atomic_write(_core.SCHEDULE_PATH, cur)


def _error_result(t: dict, drive: str | None, exc: Exception) -> dict:
    return {
        "id": _core.target_id_for(t, drive),
        "target_id": t.get("base_id", t.get("id")),
        "rule_id": "target:" + str(t.get("base_id", t.get("id"))),
        "label": t.get("label", t.get("id")),
        "category": t.get("category", "其它"),
        "path": _core.expand(t.get("path", ""), drive),
        "drive": (drive or "").rstrip(":").upper(),
        "before": 0, "after": 0, "freed": 0, "deleted": 0, "failed": 0,
        "skipped": [], "seconds": 0.0,
        "renews": None,
        "error": f"内部错误：{type(exc).__name__}: {exc}",
    }
