# -*- coding: utf-8 -*-
"""配置存取基础设施（P2-1 域拆分批次 B，从 core.py 迁出）。

原子写（_atomic_write）、带 mtime 缓存的 JSON 读取（_load_json）、
进程内可重入锁（_lock）与跨进程文件锁（_cross_process_lock）。
core.py 通过 re-import 取得同一份对象 —— 字典/锁按**对象共享**，
跨模块读写一致；core 内不存在对这些名字的重绑定（已核查）。

对 core 的反向依赖（RUNTIME_DIR / IS_WINDOWS）走 `_core` 惰性引用：
core 在自身初始化早期 import 本模块，本模块在**调用期**才访问 core
属性（此时 core 已全量初始化），且 monkeypatch（core.X = stub）
对 store 内调用点可见。
"""

from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import base64
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import ocleaner.core as _core

_cache: dict = {}
_lock = threading.RLock()
# 可重入锁：HTTP 路由层（2026-11-05 P2-3）把 config 读-改-写整段
# 包进 _lock，路由体内嵌套调用的 append_history 等也拿同一把锁 ——
# threading.Lock 不可重入会当场死锁，必须用 RLock。


@contextmanager
def _cross_process_lock(name: str, timeout: float = 3.0):
    """跨进程互斥（同一份数据被两个实例同时改写时用）。

    为什么需要：`_lock` 只在**本进程**内有效。用户双开两个实例（各自端口）
    时，两边的 append_history 都是「读 history.json → 改 → 写回」，
    交错执行就会丢记录 —— 而且丢的是清理凭据，事后无法追溯删了什么。

    Windows 用 msvcrt 的字节区间锁，其他平台用 fcntl.flock。锁文件常驻
    数据目录（只创建不删除，避免触发本项目的安全删除钩子）。

    超时后**放行**而不是报错：宁可极小概率丢一条历史，也不能因为别人
    持锁就把用户的清理卡死。
    """
    path = _core.RUNTIME_DIR / f".oc-{name}.lock"
    fh = None
    locked = False
    try:
        try:
            _core.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+b")
        except OSError:
            yield          # 连锁文件都建不了（只读目录）：降级为无锁
            return
        deadline = time.time() + timeout
        while True:
            try:
                if _core.IS_WINDOWS:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.02)
        yield
    finally:
        if fh is not None:
            if locked:
                try:
                    if _core.IS_WINDOWS:
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass


def _load_json(path: Path, default, use_cache: bool = True):
    """读 JSON。utf-8-sig 容错记事本保存的 BOM；带 mtime 缓存。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    if use_cache and _cache.get(key, (None, None))[1] == mtime:
        return _cache[key][0]
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return default
    if use_cache:
        _cache[key] = (data, mtime)
    return data


def _atomic_write(path: Path, data) -> None:
    """原子写 JSON：先写临时文件再 os.replace，避免读到半个文件。

    临时文件名必须唯一（带 pid + 线程 id + 随机串），不能用固定的 `.tmp` 后缀：
    并发写同一个目标文件时，两个线程会同时打开**同一个** tmp 并互相覆盖，
    随后 os.replace 争抢同一个源路径，Windows 上直接抛
    PermissionError: [WinError 5] 拒绝访问。实测并发 POST /__oc__/schedule
    20 路时有 3 路 500（调用点虽然持了 _lock，但 _atomic_write 还被
    append_history 等其他路径调用，锁不住所有组合）。

    os.replace 在 Windows 上偶发共享冲突（杀软/索引器短暂持有目标文件），
    这里做几次短重试 —— 写配置失败是绝对不可接受的。
    """
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex[:8]}.tmp")
    try:
        # 父目录不存在就先建：用户用 --data-dir 指向一个全新空目录
        # （数据迁移场景）时，第一次保存配置不该因为「config/ 还没建」
        # 而抛 FileNotFoundError。以前这里直接 open 会失败。
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        last: Exception | None = None
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:   # WinError 5 / 32：被短暂占用
                last = exc
                time.sleep(0.02 * (attempt + 1))
        raise last if last else RuntimeError("os.replace 失败")
    finally:
        # 成功时 tmp 已被 replace 掉；失败时清理残留，避免在仓库里堆垃圾
        # （本项目的安全删除钩子会把这类残留当删除目标拦下，见 HANDOFF）
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        except SystemExit:
            pass


_UNIT_MULT = {"b": 1, "kb": 1024, "mb": 1024 ** 2,
              "gb": 1024 ** 3, "tb": 1024 ** 4}


def _bundle_unit_bytes(u: dict) -> int | None:
    """导入单位时算出字节数：接受 bytes_per_unit 或 size:{value,unit}。"""
    bpu = u.get("bytes_per_unit")
    if bpu is not None:
        try:
            bpu = float(bpu)
        except (TypeError, ValueError):
            return None
        if bpu > 0 and bpu != float("inf"):
            return int(bpu)
        return None
    size = u.get("size")
    if isinstance(size, dict):
        try:
            val = float(size.get("value"))
            mult = _UNIT_MULT.get(str(size.get("unit") or "").lower())
        except (TypeError, ValueError):
            return None
        if val > 0 and mult:
            return int(val * mult)
    return None


def _export_bundle() -> dict:
    """把本机可迁移的自定义项汇成一个 JSON（bundle/import 的逆操作）。"""
    cfg = _core.load_convert()
    units = [{
        "id": u.get("id"), "label": u.get("label"),
        "plural": u.get("plural") or u.get("label"),
        "bytes_per_unit": u.get("bytes_per_unit"),
        "note": u.get("note", ""), "category": u.get("category", "自定义"),
    } for u in cfg.get("units", []) if isinstance(u, dict)]
    return {
        "kind": "ocleaner-custom",
        "version": 1,
        "exported": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "units": units,
        "overrides": _core.load_overrides().get("targets", {}),
        "rules": _core.load_custom_rules(),
        "fs_groups": _core.load_fs_groups(),
    }


def _import_bundle(body: dict) -> dict:
    """自定义数据包导入的落盘逻辑（供路由与云同步共用）：逐项校验并合并进本地文件。"""
    if str((body or {}).get("kind") or "") != "ocleaner-custom":
        raise ValueError("这不是 O-Cleaner 自定义数据包"
                         "（kind 应为 ocleaner-custom）")
    rep = {"units_added": 0, "units_updated": 0, "units_skipped": [],
           "overrides_applied": 0, "overrides_skipped": [],
           "rules_added": 0, "rules_updated": 0, "rules_skipped": [],
           "groups_added": 0, "groups_updated": 0}

    cfg = _core.load_convert()
    units = list(cfg.get("units", []))
    by_id = {str(u.get("id") or ""): u for u in units}
    by_label = {str(u.get("label") or ""): u for u in units}
    for i, u in enumerate(body.get("units") or []):
        if not isinstance(u, dict):
            continue
        label = str(u.get("label") or "").strip()
        nbytes = _bundle_unit_bytes(u)
        if not label:
            rep["units_skipped"].append(f"units[{i}] 缺名称")
            continue
        if nbytes is None:
            rep["units_skipped"].append(
                f"「{label}」缺有效大小（bytes_per_unit 或 size.value/unit）")
            continue
        uid = str(u.get("id") or "").strip()
        entry = {"label": label,
                 "plural": str(u.get("plural") or label).strip() or label,
                 "bytes_per_unit": nbytes,
                 "note": str(u.get("note") or "").strip(),
                 "category": str(u.get("category") or "自定义").strip()}
        if uid and uid in by_id:
            by_id[uid].update(entry)
            rep["units_updated"] += 1
        elif label in by_label:
            rep["units_skipped"].append(f"「{label}」已有同名单位")
        else:
            uid = uid or f"u{int(time.time() * 1000) % 10 ** 10}{i}"
            entry["id"] = uid
            units.append(entry)
            by_id[uid] = entry
            by_label[label] = entry
            rep["units_added"] += 1
    cfg["units"] = units
    _atomic_write(_core.CONVERT_PATH, cfg)
    _cache.pop(str(_core.CONVERT_PATH), None)

    data = _core.load_overrides()
    for k, v in (body.get("overrides") or {}).items():
        k = str(k).strip()
        _is_path_key = bool(re.match(r"^[a-zA-Z]:\\", k)) and len(k) <= 500
        if (not k or len(k) > (500 if _is_path_key else 200)
                or "/" in k or ("\\" in k and not _is_path_key)):
            rep["overrides_skipped"].append(f"「{k[:60]}」键不合法")
            continue
        entry = None
        if isinstance(v, dict):
            entry = {}
            for f in ("what", "rebuild", "impact", "keep_if"):
                t = v.get(f)
                if isinstance(t, str) and t.strip():
                    if len(t) > 2000:
                        entry = None
                        break
                    entry[f] = t.strip()
            for f in ("label", "note"):
                t = v.get(f)
                if isinstance(t, str) and t.strip() and len(t) <= 200:
                    entry[f] = t.strip()
            if isinstance(v.get("cleanable"), bool):
                entry["cleanable"] = v["cleanable"]
            if isinstance(v.get("pin"), bool):
                entry["pin"] = v["pin"]
            if v.get("label_kind") in ("clean", "unk", "keep"):
                entry["label_kind"] = v["label_kind"]
            if isinstance(v.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v["color"]):
                entry["color"] = v["color"].lower()
            if not entry:
                entry = None
        if entry is None:
            rep["overrides_skipped"].append(f"「{k}」没有可用的字段")
            continue
        data["targets"][k] = entry
        rep["overrides_applied"] += 1
    _core.save_overrides(data)

    rules = _core.load_custom_rules()
    valid_cache: dict = {}
    for i, r0 in enumerate(body.get("rules") or []):
        if not isinstance(r0, dict):
            continue
        name = str(r0.get("name") or "").strip()
        ids = [str(x).strip() for x in (r0.get("ids") or []) if str(x).strip()]
        drive = (str(r0.get("drive") or "C")).rstrip(":").upper() or "C"
        if not name:
            rep["rules_skipped"].append(f"rules[{i}] 缺规则名")
            continue
        if not ids:
            rep["rules_skipped"].append(f"「{name}」没有勾选任何清理项")
            continue
        if drive not in valid_cache:
            valid_cache[drive] = {t["id"]
                                  for t in _core.build_targets(drive, None)}
        bad = [x for x in ids if x not in valid_cache[drive]]
        if bad:
            rep["rules_skipped"].append(
                f"「{name}」含 {len(bad)} 个当前不存在的清理项"
                f"（如 {bad[0]}）")
            continue
        existing = next((r for r in rules if r["name"] == name), None)
        if existing:
            existing["ids"] = ids
            existing["drive"] = drive
            existing["auto"] = bool(r0.get("auto"))
            rep["rules_updated"] += 1
        else:
            rules.append({
                "id": f"rule-{int(time.time() * 1000) % 10 ** 10}{i}",
                "name": name, "ids": ids, "drive": drive,
                "auto": bool(r0.get("auto")),
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            rep["rules_added"] += 1
    _core.save_custom_rules(rules)

    if isinstance(body.get("fs_groups"), list):
        groups = _core.load_fs_groups()
        by_gname = {str(g.get("name") or ""): g for g in groups if isinstance(g, dict)}
        for g0 in body.get("fs_groups") or []:
            if not isinstance(g0, dict):
                continue
            gname = str(g0.get("name") or "").strip()[:60]
            gpaths = [str(x).replace("/", "\\").rstrip("\\").strip().lower()
                      for x in (g0.get("paths") or []) if str(x).strip()]
            if not gname or not gpaths:
                continue
            if gname in by_gname:
                merged = list(dict.fromkeys((by_gname[gname].get("paths") or []) + gpaths))
                by_gname[gname]["paths"] = merged[:200]
                rep["groups_updated"] += 1
            else:
                groups.append({
                    "id": str(g0.get("id") or f"grp-{int(time.time() * 1000) % 10 ** 10}"),
                    "name": gname,
                    "drive": str(g0.get("drive") or "").rstrip(":").upper(),
                    "paths": list(dict.fromkeys(gpaths))[:200],
                })
                rep["groups_added"] += 1
        _core.save_fs_groups(groups)
    return rep


_BACKUP_CFG_FILES = (
    "custom-rules.json", "custom-overrides.json", "fs-groups.json",
    "convert.json", "game-libs.json", "packs-state.json",
)


def _iter_portable_files(data_dir: Path):
    """列举可安全跨机迁移的 (相对路径, 绝对路径)，跳过敏感凭据与锁文件。"""
    cfg_dir = data_dir / "config"
    for name in _BACKUP_CFG_FILES:
        fp = cfg_dir / name
        if fp.is_file():
            yield f"config/{name}", fp
    upacks = cfg_dir / "packs-user"
    if upacks.is_dir():
        for fp in upacks.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(data_dir).as_posix()
                yield rel, fp
    rt_dir = data_dir / "data"
    if rt_dir.is_dir():
        for fp in rt_dir.rglob("*"):
            if not fp.is_file():
                continue
            if fp.name.startswith(".oc-") and fp.name.endswith(".lock"):
                continue
            rel = fp.relative_to(data_dir).as_posix()
            yield rel, fp


def export_full_backup() -> bytes:
    """把本机配置与历史/趋势/快照打包成便携 zip（不含本机 DPAPI 密钥）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, fp in _iter_portable_files(_core.DATA_DIR):
            try:
                z.write(fp, rel)
            except OSError:
                continue
    return buf.getvalue()


def import_full_backup(raw_bytes: bytes) -> dict:
    """从备份 zip 还原配置与运行数据到当前 DATA_DIR（拒绝路径穿越与密钥覆盖）。"""
    if not raw_bytes:
        raise ValueError("备份文件为空")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"不是合法的 ZIP 备份文件：{exc}") from exc
    copied = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename.replace("\\", "/").lstrip("/")
            if ".." in rel.split("/") or ":" in rel:
                continue
            if not (rel.startswith("config/") or rel.startswith("data/")):
                continue
            if rel in ("config/ai.json", "config/sync.json"):
                continue
            if info.file_size > 32 * 1024 * 1024:
                continue
            dst = (_core.DATA_DIR / rel).resolve()
            if not str(dst).startswith(str(_core.DATA_DIR.resolve())):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f:
                dst.write_bytes(src_f.read())
            copied += 1
    _cache.clear()
    return {"ok": True, "files": copied}


def migrate_data_dir(target_dir: str, reset: bool = False) -> dict:
    """图形化迁移或恢复数据目录（无需 --data-dir 命令行参数）。"""
    ptr = _core.BASE_DIR / ".ocleaner-data-dir"
    if reset:
        try:
            if ptr.exists():
                ptr.unlink()
        except OSError as exc:
            raise ValueError(f"无法恢复默认目录：{exc}") from exc
        return {"ok": True, "data_dir": str(_core.BASE_DIR),
                "copied": 0, "need_restart": True}

    raw = str(target_dir or "").strip()
    if not raw or not os.path.isabs(raw):
        raise ValueError("请输入完整的绝对目录路径（例如 D:\\O-Cleaner-Data）")
    dst = Path(raw).resolve()
    if len(dst.parts) <= 1:
        raise ValueError("不能直接使用盘符根目录，请指定子目录（例如 D:\\O-Cleaner-Data）")
    try:
        dst.mkdir(parents=True, exist_ok=True)
        probe = dst / f".oc_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"目标目录不可写：{exc}") from exc

    copied = 0
    if dst != _core.DATA_DIR.resolve():
        for rel, fp in _iter_portable_files(_core.DATA_DIR):
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, out)
            copied += 1
    ptr.write_text(str(dst), encoding="utf-8")
    return {"ok": True, "data_dir": str(dst),
            "copied": copied, "need_restart": True}
