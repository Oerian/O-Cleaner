# -*- coding: utf-8 -*-
"""O-Cleaner 快速单元测试（纯逻辑、无磁盘写副作用、2 秒内跑完）。

定位（外部审计 P2-1/二期建议的落地）：selftest.py 是含真实删除与
HTTP 端到端的全量套件（权威验证走 CI）；本文件收录**无副作用**的
纯逻辑断言 —— 供开发机上随时高频回归（改完代码先跑这个再跑门禁）。

运行：python -X utf8 tools/unit_test.py
退出码：0 = 全部通过；1 = 有失败。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

import ocleaner.core as app  # noqa: E402 —— P2-1：实现主体在 ocleaner/core.py，unit_test 的 monkeypatch 目标必须在 core 模块上

_checks: list = []


def check(name: str, cond, detail: str = "") -> None:
    _checks.append((name, bool(cond), detail))
    print(("  [OK]   " if cond else "  [FAIL] ") + name
          + (f"   {detail}" if (detail and not cond) else ""))


def main() -> int:
    # ---- HTTP 入口守卫（P1-1）----
    _fake = type("F", (), {"headers": {}})()
    _fake.headers = {"Host": "127.0.0.1:8770",
                     "Origin": "http://127.0.0.1:8770"}
    check("入口守卫：合法 Host+Origin 双向放行",
          app.Handler._host_ok(_fake) and app.Handler._origin_ok(_fake))
    for hdr, name in [
        ({"Host": "evil.example.com:8770"}, "DNS Rebinding Host"),
        ({"Host": "127.0.0.1:8770", "Origin": "null"}, "Origin: null"),
        ({"Host": "127.0.0.1:8770",
          "Origin": "http://evil.example.com"}, "跨站 Origin"),
        ({"Host": ""}, "缺失 Host"),
        ({"Host": "[::1]:8770"}, "IPv6 回环（应放行）"),
    ]:
        _fake.headers = hdr
        if name == "IPv6 回环（应放行）":
            check("入口守卫：" + name,
                  app.Handler._host_ok(_fake) and app.Handler._origin_ok(_fake),
                  str(hdr))
        else:
            check("入口守卫拒绝：" + name,
                  (not app.Handler._host_ok(_fake))
                  or (not app.Handler._origin_ok(_fake)), str(hdr))

    # ---- DPAPI 凭据加密（P1-4）----
    _k = "unit-dpapi-" + str(int(time.time()))
    _enc = app._dpapi_protect(_k)
    check("DPAPI：密文带 dpapi: 前缀且不含明文",
          _enc.startswith("dpapi:") and _k not in _enc, _enc[:32])
    check("DPAPI：同机解密往返一致", app._dpapi_unprotect(_enc) == _k)
    check("DPAPI：畸形密文安全返回空串",
          app._dpapi_unprotect("dpapi:!!not-b64!!") == "")
    check("DPAPI：明文旧值原样返回", app._dpapi_unprotect(_k) == _k)
    check("DPAPI：空串不加密", app._dpapi_protect("") == "")

    # ---- fetch_json 目标收口（P1-3）----
    _e1 = app.fetch_json("http://127.0.0.1:9/index.json")[1]
    check("fetch_json：回环地址入口拒绝", bool(_e1) and "拒绝" in _e1, str(_e1))
    _e2 = app.fetch_json("http://metadata.google.internal/x")[1]
    check("fetch_json：云元数据拒绝", bool(_e2) and "拒绝" in _e2, str(_e2))
    _e3 = app.fetch_json("ftp://example.com/x")[1]
    check("fetch_json：非 http/https 拒绝", bool(_e3), str(_e3))
    _u = app._is_unsafe_remote("http://user:pass@lan/x")
    check("_is_unsafe_remote：带凭据 URL 拒绝", bool(_u), str(_u))
    check("_is_unsafe_remote：局域网地址放行（LAN 文件服务器场景）",
          not app._is_unsafe_remote("http://192.168.1.10/packs/index.json"))

    # ---- 测量原语（P2-4）----
    with tempfile.TemporaryDirectory() as td:
        sub = os.path.join(td, "sub")
        os.makedirs(sub)
        for i in range(5):
            with open(os.path.join(td, f"f{i}.bin"), "wb") as fh:
                fh.write(b"x" * 1000)
        with open(os.path.join(sub, "g.bin"), "wb") as fh:
            fh.write(b"y" * 500)
        _base = sum(os.path.getsize(os.path.join(dp, f))
                    for dp, _, fs in os.walk(td) for f in fs)
        _r = app._py_measure_tree(td)
        check("测量：递归体积/文件数与 os.walk 基准一致",
              _r["size"] == _base and _r["files"] == 6 and not _r["denied"],
              str(_r))
        _g = app._py_measure_glob(td, "*.bin")
        check("测量：glob 过滤只计匹配文件",
              _g["files"] == 5 and _g["size"] == 5000, str(_g))
        _dk = app._py_dir_kind(td)
        check("测量：单层清单含 1 目录 5 文件、带 yyyy-MM-dd 修改日期",
              len(_dk["dirs"]) == 1 and len(_dk["files"]) == 5
              and all(len(x["modified"]) == 10
                      for x in _dk["dirs"] + _dk["files"]), str(_dk)[:120])
        check("测量：宽松 tree_size 与基准一致",
              app._py_tree_size(td) == _base)
        _t0 = time.perf_counter()
        app._py_measure_tree(r"C:\\Windows\\Tasks")
        _dt = time.perf_counter() - _t0
        check("测量：小目录进程内测量 <50ms（免 PS 冷启动）", _dt < 0.05,
              f"{_dt * 1000:.1f}ms")

    # ---- 引擎校准跨设备失效（372a1c0）----
    with tempfile.TemporaryDirectory() as td:
        _f = Path(td) / "engine_speed.json"
        _old_f = app._ENGINE_SPEED_FILE
        _old_s = dict(app._ENGINE_SPEEDS)
        app._ENGINE_SPEED_FILE = _f
        try:
            _host = app.platform.node()
            _f.write_text('{"C": {"wiztree": 5.0, "mft": 2.0, "at": 1.0},'
                          '"_pref": "wiztree", "_host": "OLD-PC"}',
                          encoding="utf-8")
            app._eng_speeds_load()
            check("引擎校准：换机加载清实测值、保留偏好",
                  "C" not in app._ENGINE_SPEEDS
                  and app._ENGINE_SPEEDS.get("_pref") == "wiztree"
                  and app._ENGINE_SPEEDS.get("_host") == _host,
                  str(app._ENGINE_SPEEDS)[:80])
        finally:
            app._ENGINE_SPEED_FILE = _old_f
            app._ENGINE_SPEEDS.clear()
            app._ENGINE_SPEEDS.update(_old_s)

    # ---- 嵌套 Junction 护栏（P1-2）----
    with tempfile.TemporaryDirectory() as td:
        _target = Path(td) / "protected"
        (_target / "keep").mkdir(parents=True)
        (_target / "keep" / "marker.txt").write_text("survive", encoding="utf-8")
        _outer = Path(td) / "outer"
        (_outer / "sub").mkdir(parents=True)
        _link = _outer / "sub" / "j_link"
        try:
            import _winapi
            _winapi.CreateJunction(str(_target), str(_link))
            _jok = _link.exists()
        except Exception:
            _jok = False
        if _jok:
            _full, _partial, _note = app.rm_tree_strict(str(_outer))
            _marker = _target / "keep" / "marker.txt"
            check("Junction 护栏：挂载点摘除、目标内容完好",
                  _full and not _link.exists() and _marker.exists()
                  and _marker.read_text(encoding="utf-8") == "survive",
                  f"full={_full} partial={_partial}")
        else:
            print("  [SKIP] Junction 护栏（本机无法创建 junction）")

    # ---- P0 护栏回归：allow_user_data=True 不得绕过自保护与 Junction 复检 ----
    orig_base = app.BASE_DIR
    desktop_dir = Path(os.path.expanduser("~")) / "Desktop"
    if desktop_dir.is_dir():
        try:
            app.BASE_DIR = desktop_dir
            ok_self, why_self = app.check_deletable(str(desktop_dir), "tree",
                                                    allow_user_data=True)
            check("护栏自保护：allow_user_data=True 仍拒绝 BASE_DIR",
                  not ok_self and "自身" in why_self, f"{ok_self} {why_self}")
        finally:
            app.BASE_DIR = orig_base
    with tempfile.TemporaryDirectory(prefix="oc_ut_jguard_") as td:
        # 模拟受保护目录名（Documents）实为指向 C:\Windows 的 Junction
        _jdocs = Path(td) / "Documents"
        _win_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        if _win_dir.is_dir():
            try:
                import _winapi
                _winapi.CreateJunction(str(_win_dir), str(_jdocs))
                _jok2 = _jdocs.exists()
            except Exception:
                _jok2 = False
            if _jok2:
                try:
                    ok_j, why_j = app.check_deletable(str(_jdocs), "tree",
                                                      allow_user_data=True)
                    check("护栏 Junction 复检：allow_user_data=True 仍拒绝指向 Windows 的链接",
                          not ok_j, f"{ok_j} {why_j}")
                finally:
                    try:
                        os.rmdir(str(_jdocs))
                    except OSError:
                        pass

    # ---- Python 3.8/3.9 兼容守卫：ocleaner/*.py 使用 PEP 585/604 注解必带 __future__ ----
    import ast as _ast
    _repo_root = Path(__file__).resolve().parent.parent
    missing_future = []
    for _mp in sorted((_repo_root / "ocleaner").glob("*.py")):
        _tree = _ast.parse(_mp.read_text(encoding="utf-8"), str(_mp))
        _has_f = any(isinstance(n, _ast.ImportFrom) and n.module == "__future__"
                     and any(a.name == "annotations" for a in n.names)
                     for n in _tree.body)
        if _has_f:
            continue
        for _node in _ast.walk(_tree):
            if isinstance(_node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                _anns = [a.annotation for a in _node.args.args + _node.args.kwonlyargs if a.annotation]
                if _node.returns:
                    _anns.append(_node.returns)
                for _ann in _anns:
                    for _sub in _ast.walk(_ann):
                        if (isinstance(_sub, _ast.Subscript) and isinstance(_sub.value, _ast.Name)
                                and _sub.value.id in ("list", "dict", "tuple", "set", "frozenset", "type")) \
                                or (isinstance(_sub, _ast.BinOp) and isinstance(_sub.op, _ast.BitOr)):
                            missing_future.append(_mp.name)
                            break
    check("兼容守卫：ocleaner 子模块 PEP 585/604 注解均含 future annotations",
          not missing_future, f"missing={sorted(set(missing_future))}")

    # ---- 卸载注册表键白名单校验 ----
    check("注册表白名单：合法 HKLM Uninstall 放行",
          app._parse_uninstall_reg_key(
              r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\App1")
          == ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\App1"))
    check("注册表白名单：合法 HKCU Uninstall 放行",
          app._parse_uninstall_reg_key(
              r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\App2")
          == ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\App2"))
    check("注册表白名单：拒绝非 Uninstall 子树（Run）",
          app._parse_uninstall_reg_key(
              r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run\Evil") is None)
    check("注册表白名单：拒绝 .. 目录穿越",
          app._parse_uninstall_reg_key(
              r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\..\Run\Evil") is None)

    # ---- 下载归档不误套已有子目录 ----
    with tempfile.TemporaryDirectory(prefix="oc_ut_dl_") as td:
        (Path(td) / "安装包").mkdir()
        (Path(td) / "demo.exe").write_bytes(b"MZ" + b"\x00" * 64)
        orig_log = app.ARCHIVE_LOG_PATH
        try:
            app.ARCHIVE_LOG_PATH = Path(td) / ".archive-log.json"
            ar = app.downloads_archive(root=td, only=None)
            check("下载归档：一键归档只移动顶层文件、不嵌套已有子目录",
                  ar.get("ok") and ar.get("moved") == 1
                  and (Path(td) / "安装包").is_dir()
                  and not (Path(td) / "未识别" / "安装包").exists(),
                  f"{ar}")
        finally:
            app.ARCHIVE_LOG_PATH = orig_log

    # ---- 外置签名库（appclean / bloatware-builtin）加载与安全边界 ----
    ac_apps = app._load_appclean_apps()
    check("应用专清外置配置：config/appclean.json 已加载且含微信4/开发者缓存",
          len(ac_apps) >= 15 and any(a.get("id") == "wechat4" for a in ac_apps),
          f"count={len(ac_apps)}")
    ai_cache_leak = [a["id"] for a in ac_apps
                     if a.get("id") in ("huggingface", "ollama")
                     and any(it.get("kind") == "cache" for it in (a.get("items") or []))]
    check("应用专清安全边界：AI 模型目录不标为 cache",
          not ai_cache_leak, f"leaked={ai_cache_leak}")
    bw_apps = app._load_bloatware().get("apps") or []
    check("毒瘤清除外置配置：config/bloatware-builtin.json 已加载",
          len(bw_apps) >= 15, f"count={len(bw_apps)}")

    # ---- 回执档案 API：正常读取 + 路径穿越拒绝 ----
    with tempfile.TemporaryDirectory(prefix="oc_ut_rcp_") as td:
        orig_rdir = app.RECEIPT_DIR
        try:
            app.RECEIPT_DIR = Path(td)
            app._write_receipt({
                "time": "2026-11-21 10:00:00", "drive": "D",
                "trigger": "手动清理（D:）", "freed": 2048, "freed_h": "2.0 KB",
                "failed": 0, "targets": 1,
                "results": [{"label": "DemoCache", "path": r"D:\Demo\Cache", "freed": 2048}],
            })
            lr = app.list_receipts(q="DemoCache", drive="D")
            check("回执检索：按关键词与盘符命中刚落盘的回执",
                  lr.get("ok") and lr.get("total") == 1, f"{lr}")
            nm = lr["receipts"][0]["name"]
            rd, rerr = app.read_receipt(nm)
            check("回执读取：合法文件名可读出完整载荷",
                  rd is not None and not rerr and rd.get("freed") == 2048, f"{rerr}")
            bad1, err1 = app.read_receipt("../history.json")
            bad2, err2 = app.read_receipt(r"..\targets.json")
            check("回执安全：拒绝路径穿越文件名",
                  bad1 is None and bad2 is None and bool(err1) and bool(err2),
                  f"{err1} / {err2}")
        finally:
            app.RECEIPT_DIR = orig_rdir

    # ---- 版本一致性 ----
    import json
    dv = json.load(open("config/drives.json", encoding="utf-8")) \
        .get("app", {}).get("version")
    check("版本一致性：app.py 与 drives.json", dv == app.APP_VERSION,
          f"app={app.APP_VERSION} drives={dv}")

    # ---- 磁盘趋势预测（v2.18 Task 3）：线性回归 + 预警分级 ----
    import json as _json_t
    from datetime import datetime as _dt, timedelta as _td
    orig_trend = app.DISK_TREND_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="oc_ut_trend_") as td:
            tp = Path(td) / "disk-trend.json"
            app.DISK_TREND_PATH = tp

            def _mk_trend(rows):
                # rows: [(days_ago, free_bytes)]，构造 disk-trend.json 结构
                today = _dt.now().date()
                hist = []
                for age, free in rows:
                    hist.append({"d": (today - _td(days=age)).strftime("%Y-%m-%d"),
                                 "u": {"X": {"total": 100 << 30, "free": free}}})
                return hist

            # 1) 样本不足（3 天）：不预测、如实说明
            tp.write_text(_json_t.dumps(
                _mk_trend([(0, 20 << 30), (1, 22 << 30), (2, 24 << 30)]),
                ensure_ascii=False), encoding="utf-8")
            r1 = app.trend_forecast("X")
            check("趋势预测：样本不足时 days_left=None 且 samples 如实上报",
                  r1.get("days_left") is None and r1.get("samples") == 3
                  and "样本不足" in str(r1.get("reason")), f"{r1}")

            # 2) 剩余空间稳定（10 天不变）：不预测
            tp.write_text(_json_t.dumps(
                _mk_trend([(a, 10 << 30) for a in range(10)]),
                ensure_ascii=False), encoding="utf-8")
            r2 = app.trend_forecast("X")
            check("趋势预测：空间稳定时 days_left=None",
                  r2.get("days_left") is None and r2.get("level") == "none",
                  f"{r2}")

            # 3) 线性下降 2GB/天、最新剩 20GB：days_left=10、crit 级
            tp.write_text(_json_t.dumps(
                _mk_trend([(a, (20 + 2 * a) << 30) for a in range(10)]),
                ensure_ascii=False), encoding="utf-8")
            r3 = app.trend_forecast("X")
            check("趋势预测：线性下降 days_left 按回归斜率计算",
                  r3.get("days_left") == 10 and r3.get("level") == "crit"
                  and r3.get("slope_per_day") == -(2 << 30), f"{r3}")

            # 4) 温和下降 0.5GB/天、剩 60GB：days_left=120、info 级（不上横幅）
            tp.write_text(_json_t.dumps(
                _mk_trend([(a, int((60 + 0.5 * a) * (1 << 30))) for a in range(10)]),
                ensure_ascii=False), encoding="utf-8")
            r4 = app.trend_forecast("X")
            check("趋势预测：30 天外为 info 级（不触发预警横幅）",
                  r4.get("days_left") == 120 and r4.get("level") == "info",
                  f"{r4}")

            # 5) 空间回升（负斜率→正向）：不预测
            tp.write_text(_json_t.dumps(
                _mk_trend([(a, (60 - 2 * a) << 30) for a in range(10)]),
                ensure_ascii=False), encoding="utf-8")
            r5 = app.trend_forecast("X")
            check("趋势预测：剩余空间回升时 days_left=None",
                  r5.get("days_left") is None, f"{r5}")

            # 6) 响应结构完整：前端横幅依赖的字段齐全
            check("趋势预测：响应含 hot_dirs 列表（无快照时为空）",
                  isinstance(r5.get("hot_dirs"), list)
                  and isinstance(r5.get("free_h"), str), f"{r5}")
    finally:
        app.DISK_TREND_PATH = orig_trend

    # ---------- HTTP 路由参数校验与边界 ----------
    import http.server as _hs
    import threading as _th
    import urllib.error as _ue
    import urllib.request as _ur

    srv = _hs.HTTPServer(("127.0.0.1", 0), app.Handler)
    port = srv.server_address[1]
    t_srv = _th.Thread(target=srv.serve_forever, daemon=True)
    t_srv.start()
    try:
        def _req(method, path, body=None, ctype="application/json"):
            req = _ur.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method)
            if body is not None:
                req.add_header("Content-Type", ctype)
            try:
                with _ur.urlopen(req, timeout=3) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except _ue.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        st1, b1 = _req("GET", "/__oc__/ext-cache?drive=INVALID")
        check("HTTP路由：ext-cache 非法盘符返回 400", st1 == 400 and "drive" in b1, f"{st1} {b1}")

        st2, _ = _req("GET", "/__oc__/receipt?name=../../secret.json")
        check("HTTP路由：receipt 路径穿越返回 400", st2 == 400, f"status={st2}")

        st3, _ = _req("GET", "/__oc__/nonexistent_route")
        check("HTTP路由：未知 GET 路由返回 404", st3 == 404, f"status={st3}")

        st4, _ = _req("POST", "/__oc__/schedule", body=b"{not-json")
        check("HTTP路由：POST 非法 JSON 返回 400", st4 == 400, f"status={st4}")

        st5, _ = _req("POST", "/__oc__/nonexistent_post", body=b"{}")
        check("HTTP路由：未知 POST 路由返回 404", st5 == 404, f"status={st5}")

        st6, b6 = _req("GET", "/favicon.ico")
        check("HTTP路由：favicon.ico 返回 200 且非空", st6 == 200 and len(b6) > 100, f"status={st6} len={len(b6)}")

        st7, _ = _req("POST", "/__oc__/process/stop", body=b"{}")
        check("HTTP路由：process/stop 空 pids 返回 400", st7 == 400, f"status={st7}")
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------- 进程停止护栏 ----------
    r_stop = app.stop_lock_processes([0, 4, os.getpid()])
    check("进程停止护栏：拒绝 PID<=4 与自身 PID",
          r_stop.get("ok") and len(r_stop.get("skipped") or []) == 3
          and not r_stop.get("stopped"), f"{r_stop}")

    # ---------- 自定义数据打包导入/导出（含 fs_groups） ----------
    orig_fg_path = app.FS_GROUPS_PATH
    try:
        with tempfile.TemporaryDirectory() as td_fg:
            app.FS_GROUPS_PATH = Path(td_fg) / "fs-groups.json"
            imp_rep = app._import_bundle({
                "kind": "ocleaner-custom",
                "fs_groups": [{"name": "TestGrp", "drive": "D", "paths": ["D:\\Foo", "D:\\Bar"]}],
            })
            exp_b = app._export_bundle()
            check("自定义包导入导出：包含 fs_groups 往返",
                  imp_rep.get("groups_added") == 1
                  and len(exp_b.get("fs_groups") or []) == 1
                  and exp_b["fs_groups"][0]["name"] == "TestGrp",
                  f"imp={imp_rep} exp={exp_b.get('fs_groups')}")
    finally:
        app.FS_GROUPS_PATH = orig_fg_path

    # ---------- AI 批量识别与本地缓存 ----------
    import ocleaner.integrations as _integ
    orig_ai_cache = app.AI_CACHE_PATH
    orig_ai_chat = _integ.ai_chat
    try:
        with tempfile.TemporaryDirectory() as td_ai:
            app.AI_CACHE_PATH = Path(td_ai) / "ai-cache.json"
            r_empty = app.ai_explain_batch({"items": []})
            check("AI批量识别：空列表返回错误", not r_empty.get("ok"), f"{r_empty}")

            calls = [0]
            def _fake_chat(msgs, timeout=None):
                calls[0] += 1
                return True, '{"items":[{"key":"D:\\\\FooCache","title":"Foo 缓存","category":"缓存","what":"临时缓存","rebuild":"可重建","cleanable":true,"advice":"可安全清理"}]}'
            _integ.ai_chat = _fake_chat

            r_b1 = app.ai_explain_batch({"items": [{"path": "D:\\FooCache", "name": "FooCache", "kind": "dir", "size_h": "1.2 GB"}]})
            check("AI批量识别：首次查询解析并落盘缓存",
                  r_b1.get("ok") and r_b1.get("fetched") == 1 and r_b1.get("from_cache") == 0
                  and r_b1["results"]["D:\\FooCache"]["title"] == "Foo 缓存", f"{r_b1}")

            r_b2 = app.ai_explain_batch({"items": [{"path": "D:\\FooCache", "name": "FooCache", "kind": "dir", "size_h": "1.2 GB"}]})
            check("AI批量识别：二次查询命中本地缓存（零模型调用）",
                  r_b2.get("ok") and r_b2.get("from_cache") == 1 and r_b2.get("fetched") == 0 and calls[0] == 1,
                  f"{r_b2} calls={calls[0]}")
    finally:
        app.AI_CACHE_PATH = orig_ai_cache
        _integ.ai_chat = orig_ai_chat

    # ---------- MFT 原生加速桥接与回退 ----------
    import ocleaner.engines as _eng
    c_src = Path(app.BASE_DIR) / "ocleaner" / "mft_fast.c"
    dll_obj = _eng._load_mft_fast()
    check("MFT 原生加速：mft_fast.c 就位且缺失 DLL 时安全回退 Python",
          c_src.is_file() and (dll_obj is None or dll_obj.mft_fast_version() == 5),
          f"c_src={c_src.is_file()} dll={dll_obj}")

    # ---------- 社区规则一键上传（无令牌 Issue 模式） ----------
    orig_export = app._export_bundle
    orig_sync_cfg = _integ._sync_cfg
    try:
        _integ._sync_cfg = lambda: {}
        app._export_bundle = lambda: {"kind": "ocleaner-custom"}
        empty_err = False
        try:
            _integ.community_upload({"scope": "all"})
        except ValueError:
            empty_err = True
        check("社区上传：空自定义数据时拒绝提交", empty_err)

        app._export_bundle = lambda: {
            "kind": "ocleaner-custom",
            "overrides": {"D:\\TestCache": {"label": "测试缓存", "cleanable": True, "label_kind": "clean"}},
            "units": [{"label": "张照片", "bytes_per_unit": 5242880}],
        }
        up_res = _integ.community_upload({"scope": "all", "title": "测试规则"})
        check("社区上传：无令牌走 Issue 预填模式且产出合法 JSON",
              up_res.get("ok") and up_res.get("mode") == "issue" and up_res.get("count") == 2
              and "Oerian/O-Cleaner/issues/new" in (up_res.get("issue_url") or "")
              and "TestCache" in (up_res.get("json_text") or ""),
              f"{up_res}")
    finally:
        app._export_bundle = orig_export
        _integ._sync_cfg = orig_sync_cfg

    # ---------- 顽固占用重启删除护栏 & 开机自启查询 ----------
    orig_is_admin = app.is_admin
    try:
        app.is_admin = lambda: False
        rb_noadm = app.schedule_delete_on_reboot(["C:\\Windows\\System32\\ntdll.dll"])
        check("重启删除：非管理员明确拒绝", not rb_noadm.get("ok"))
        app.is_admin = lambda: True
        rb_sys = app.schedule_delete_on_reboot(["C:\\Windows\\System32\\ntdll.dll"])
        check("重启删除：系统保护路径被护栏拦截跳过",
              rb_sys.get("ok") and len(rb_sys.get("skipped") or []) == 1 and not rb_sys.get("scheduled"),
              f"{rb_sys}")
    finally:
        app.is_admin = orig_is_admin
    as_st = app.autostart_status()
    check("开机自启：状态查询返回合法结构", isinstance(as_st, dict) and "enabled" in as_st, f"{as_st}")

    # ---------- 整机备份导出/还原 & 目录迁移 ----------
    orig_data_dir = app.DATA_DIR
    try:
        with tempfile.TemporaryDirectory() as td_bak:
            bak_dir = Path(td_bak)
            (bak_dir / "config").mkdir(parents=True, exist_ok=True)
            (bak_dir / "data").mkdir(parents=True, exist_ok=True)
            (bak_dir / "config" / "custom-rules.json").write_text('[{"name":"r1"}]', encoding="utf-8")
            (bak_dir / "data" / "history.json").write_text('[{"ok":true}]', encoding="utf-8")
            (bak_dir / "config" / "ai.json").write_text('{"api_key":"secret"}', encoding="utf-8")
            app.DATA_DIR = bak_dir
            zbytes = app.export_full_backup()
            check("整机备份：导出包含规则历史且排除敏感密钥",
                  len(zbytes) > 0 and b"custom-rules.json" in zbytes and b"ai.json" not in zbytes)

            with tempfile.TemporaryDirectory() as td_dst:
                app.DATA_DIR = Path(td_dst)
                imp = app.import_full_backup(zbytes)
                check("整机备份：导入还原文件完整落地",
                      imp.get("ok") and imp.get("files") == 2
                      and (Path(td_dst) / "data" / "history.json").is_file(),
                      f"{imp}")
    finally:
        app.DATA_DIR = orig_data_dir

    # ---------- 社区资源包目录与本机环境匹配 ----------
    cat = _integ.packs_catalog()
    check("社区资源包目录：返回有效结构与本机推荐标签",
          cat.get("ok") and isinstance(cat.get("items"), list) and len(cat["items"]) >= 1
          and any(x.get("recommended") for x in cat["items"]),
          f"{cat.get('env')} count={len(cat.get('items') or [])}")

    # 兼容 v1 字符串数组 index.json（远程 packs 仓库格式）
    orig_uopen = app._urlopen
    orig_lps = app.load_packs_state
    try:
        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"packs":["windows-system.json","apps-common.json"]}'
        app._urlopen = lambda *a, **kw: _FakeResp()
        app.load_packs_state = lambda: {"disabled": [], "remote": app.DEFAULT_PACKS_REMOTE}
        cat_str = _integ.packs_catalog()
        check("社区资源包目录：兼容纯字符串数组 index.json",
              cat_str.get("ok") and len(cat_str.get("items") or []) == 2,
              f"{cat_str}")
    finally:
        app._urlopen = orig_uopen
        app.load_packs_state = orig_lps

    # ---------- Windows 官方组件安全瘦身 ----------
    ss_st = app.sys_slim_status()
    check("官方组件瘦身：只读状态查询返回合法结构",
          isinstance(ss_st, dict) and "hiber_on" in ss_st and "winsxs_exists" in ss_st,
          f"{ss_st}")
    bad_act = app.sys_slim_run("arbitrary_cmd")
    check("官方组件瘦身：拒绝非白名单操作", not bad_act.get("ok"), f"{bad_act}")
    orig_adm2 = app.is_admin
    try:
        app.is_admin = lambda: False
        no_adm = app.sys_slim_run("dism_cleanup")
        check("官方组件瘦身：非管理员拒绝执行", not no_adm.get("ok"), f"{no_adm}")
    finally:
        app.is_admin = orig_adm2

    # ---------- 安全加固与审计回归用例 ----------
    _ru = app.resolve_units([
        {"id": "a", "bytes_per_unit": 100},
        {"id": "b", "bytes_per_unit": "2 * a"},
        {"id": "evil", "bytes_per_unit": "().__class__.__bases__[0]"},
    ])
    _ru_map = {x["id"]: x["bytes"] for x in _ru}
    check("resolve_units：合法表达式正常求值", _ru_map.get("b") == 200.0, f"{_ru_map}")
    check("resolve_units：拒绝非算术 AST 逃逸表达式", _ru_map.get("evil") == 0.0, f"{_ru_map}")
    check("护栏加固：拒绝 Windows\\explorer.exe",
          not app.check_deletable(r"C:\Windows\explorer.exe", "files", True)[0])
    check("护栏加固：拒绝 Windows\\SystemApps",
          not app.check_deletable(r"C:\Windows\SystemApps", "tree", False)[0])
    check("护栏加固：localhost 尾点 SSRF 拒绝",
          bool(app._is_unsafe_remote("http://localhost.:8770/__oc__/state")))
    import io as _zio, zipfile as _zzf
    _zb = _zio.BytesIO()
    with _zzf.ZipFile(_zb, "w") as _zz:
        _zz.writestr("config/AI.json", "{}")
        _zz.writestr("config/targets.json", "{}")
        _zz.writestr("data/pending-deletes.json", "[]")
    _orig_dd = app.DATA_DIR
    try:
        with tempfile.TemporaryDirectory() as _td2:
            app.DATA_DIR = Path(_td2)
            _ib = app.import_full_backup(_zb.getvalue())
            check("备份导入白名单：拒绝大小写绕过 ai.json 与内置 targets.json/pending-deletes.json",
                  _ib.get("files") == 0, f"{_ib}")
    finally:
        app.DATA_DIR = _orig_dd

    # ---------- v2.18 P0/P1 深度审计回归测试（10 项） ----------
    # 1) bloatware/uninstall & sweep 读 "found" 键 + 异常时 _CLEAN_LOCK 必释放
    from http.server import ThreadingHTTPServer as _THS2
    import threading as _th2
    import urllib.request as _ur2, urllib.error as _ue2
    orig_bw_scan = app.bloatware_scan
    orig_sched_p = app.SCHEDULE_PATH
    try:
        with tempfile.TemporaryDirectory() as _td3:
            app.SCHEDULE_PATH = Path(_td3) / "schedule.json"
            app._atomic_write(app.SCHEDULE_PATH, {
                "enabled": False, "interval_hours": 24,
                "preset": "safe", "drive": "C",
                "last_summary": {"freed": 999},
            })
            _srv2 = _THS2(("127.0.0.1", 0), app.Handler)
            _srv2.busy = False
            _t2 = _th2.Thread(target=_srv2.serve_forever, daemon=True)
            _t2.start()
            _p2 = _srv2.server_port

            def _req2(method, path, body=b"{}", hdrs=None):
                h0 = {"Host": f"127.0.0.1:{_p2}", "Content-Type": "application/json"}
                if hdrs:
                    h0.update(hdrs)
                rq = _ur2.Request(f"http://127.0.0.1:{_p2}{path}",
                                  data=body if method == "POST" else None,
                                  headers=h0, method=method)
                try:
                    with _ur2.urlopen(rq, timeout=3) as rp:
                        return rp.status, rp.read().decode("utf-8", "replace")
                except _ue2.HTTPError as e:
                    return e.code, e.read().decode("utf-8", "replace")

            try:
                fake_reg = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\__OC_FAKE_BLOAT__"
                app.bloatware_scan = lambda: {"ok": True, "found": [{"reg_key": fake_reg, "residue_dirs": []}]}
                st_u, body_u = _req2("POST", "/__oc__/bloatware/uninstall",
                                     json.dumps({"reg_key": fake_reg}).encode("utf-8"))
                check("毒瘤清除：bloatware/uninstall 识别 found 列表不误报 403",
                      st_u == 404 and "注册表" in body_u, f"status={st_u} {body_u}")

                def _boom_scan():
                    raise RuntimeError("injected scan failure")
                app.bloatware_scan = _boom_scan
                st_sw, _ = _req2("POST", "/__oc__/bloatware/sweep",
                                 json.dumps({"reg_key": fake_reg, "dirs": []}).encode("utf-8"))
                acq_ok = app._CLEAN_LOCK.acquire(blocking=False)
                if acq_ok:
                    app._CLEAN_LOCK.release()
                check("并发互斥锁：bloatware/sweep 异常后 _CLEAN_LOCK 必释放",
                      acq_ok and st_sw >= 400, f"status={st_sw} acq={acq_ok}")

                # 2) POST /__oc__/schedule 拒绝 Infinity/bool 并清理 last_summary
                st_inf, _ = _req2("POST", "/__oc__/schedule",
                                  json.dumps({"interval_hours": "Infinity"}).encode("utf-8"))
                st_bool, _ = _req2("POST", "/__oc__/schedule",
                                   json.dumps({"interval_hours": True}).encode("utf-8"))
                st_ok, body_ok = _req2("POST", "/__oc__/schedule",
                                       json.dumps({"enabled": False, "interval_hours": 12}).encode("utf-8"))
                saved_sch = json.loads(app.SCHEDULE_PATH.read_text(encoding="utf-8"))
                check("定时调度：拒绝 Infinity/bool 且落盘移除 last_summary",
                      st_inf == 400 and st_bool == 400 and st_ok == 200
                      and "last_summary" not in saved_sch and saved_sch.get("interval_hours") == 12.0,
                      f"{st_inf}/{st_bool}/{st_ok} {saved_sch}")
            finally:
                _srv2.shutdown()
                _srv2.server_close()
    finally:
        app.bloatware_scan = orig_bw_scan
        app.SCHEDULE_PATH = orig_sched_p

    # 3) MFT C 零拷贝转换 + ABI v5 + _mft_agg_paths dir_total 一致性
    from array import array as _arr
    import ctypes as _ct
    _c_p = (_ct.c_int64 * 4)(0, 5, 5, 6)
    _c_f = (_ct.c_uint8 * 4)(1, 1, 0, 0)
    _ap0, _af0 = _arr("q"), _arr("B")
    _ap0.frombytes(memoryview(_c_p).cast("B")[:32])
    _af0.frombytes(memoryview(_c_f).cast("B")[:4])
    _ap_t = _arr("q", [0, 0, 0, 0, 0, 5, 5, 6, 7])
    _az_t = _arr("q", [0, 0, 0, 0, 0, 0, 0, 0, 2000])
    _af_t = _arr("B", [0, 0, 0, 0, 0, 1, 1, 1, 0])
    _dn_t = {6: "a".encode("utf-16-le"), 7: "b".encode("utf-16-le")}
    _dt0, _dp0 = _eng._mft_agg_paths(_ap_t, _az_t, _af_t, _dn_t, "C:", floor=0)
    _dt1, _dp1 = _eng._mft_agg_paths(_ap_t, _az_t, _af_t, _dn_t, "C:\\", floor=5000)
    check("MFT 加速回归：array(.cast('B')) 转化与 _mft_agg_paths dir_total 一致",
          list(_ap0) == [0, 5, 5, 6] and list(_af0) == [1, 1, 0, 0]
          and _dt0 == _dt1 and _dt0.get(7) == 2000 and _dp0.get(5) == "c:\\" and _dp1.get(5) == "c:\\",
          f"dt0={_dt0} dt1={_dt1} dp0={_dp0}")

    # 4) Win32 原生验签不受 PSModulePath 污染影响
    _orig_psmp = os.environ.get("PSModulePath")
    os.environ["PSModulePath"] = r"C:\__nonexistent_pwsh7_modules__"
    try:
        _sig_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
        _st_sig, _subj_sig = app._authenticode_info(str(_sig_exe))
        check("原生验签：WinVerifyTrust 在污染 PSModulePath 下仍验签 Valid",
              _st_sig == "Valid" and "microsoft" in _subj_sig.lower(),
              f"{_st_sig} {_subj_sig}")
    finally:
        if _orig_psmp is None:
            os.environ.pop("PSModulePath", None)
        else:
            os.environ["PSModulePath"] = _orig_psmp

    # 5) guard.py Junction 指向个人数据域保留 why_r + _realpath_strict
    with tempfile.TemporaryDirectory() as _td4:
        _tp = Path(_td4)
        check("护栏兼容：_realpath_strict 正常工作",
              os.path.normcase(app._realpath_strict(str(_tp))) == os.path.normcase(os.path.realpath(str(_tp))))
        _docs_real = Path(os.path.expandvars(r"%USERPROFILE%\Documents"))
        if _docs_real.is_dir():
            import subprocess as _sp4
            _jdir = _tp / "doc_junc"
            _r_mk = _sp4.run(
                ["cmd", "/c", "mklink", "/J", str(_jdir), str(_docs_real)],
                capture_output=True, text=True, timeout=5)
            if _r_mk.returncode == 0 and _jdir.exists():
                try:
                    _ok_j, _why_j = app.check_deletable(str(_jdir), "tree", allow_user_data=True)
                    check("护栏 Junction：指向个人数据目录时保留强制回收站说明 real_note",
                          _ok_j and "个人数据" in (_why_j or ""),
                          f"ok={_ok_j} why={_why_j!r}")
                finally:
                    _sp4.run(["cmd", "/c", "rmdir", str(_jdir)], capture_output=True)

    # 6) server.py 跨端口 CSRF 与 Sec-Fetch-Site 严格拦截
    _fake2 = type("F2", (), {"headers": {}})()
    _fake2.headers = {"Host": "127.0.0.1:8770", "Origin": "http://127.0.0.1"}
    _rej_default_port = not app.Handler._origin_ok(_fake2)
    _fake2.headers = {"Host": "127.0.0.1:8770", "Origin": "http://127.0.0.1:8770",
                      "Sec-Fetch-Site": "cross-site"}
    _rej_sfs = not app.Handler._origin_ok(_fake2)
    _fake2.headers = {"Host": "127.0.0.1:8770", "Origin": "http://127.0.0.1:8770",
                      "Sec-Fetch-Site": "same-origin"}
    _ok_sfs = app.Handler._host_ok(_fake2) and app.Handler._origin_ok(_fake2)
    check("入口守卫：拦截默认80端口跨站Origin及cross-site并放行same-origin",
          _rej_default_port and _rej_sfs and _ok_sfs,
          f"{_rej_default_port}/{_rej_sfs}/{_ok_sfs}")

    # 7) store.py 深拷贝缓存与 _atomic_write 清除同 tick 缓存
    with tempfile.TemporaryDirectory() as _td5:
        _jp = Path(_td5) / "cache_test.json"
        app._atomic_write(_jp, {"k": [1, 2]})
        _loaded1 = app._load_json(_jp, {}, use_cache=True)
        _loaded1["k"].append(999)
        _loaded2 = app._load_json(_jp, {}, use_cache=True)
        app._atomic_write(_jp, {"k": [3]})
        _in_cache_after = str(_jp) in app._cache
        _loaded3 = app._load_json(_jp, {}, use_cache=True)
        check("存储缓存：深拷贝隔离修改且 _atomic_write 立即清理缓存",
              _loaded2 == {"k": [1, 2]} and not _in_cache_after and _loaded3 == {"k": [3]},
              f"l2={_loaded2} in_cache={_in_cache_after} l3={_loaded3}")

    # 8) integrations.py 脱敏与非法 gist_id 拒绝
    orig_exp2, orig_sc2 = app._export_bundle, _integ._sync_cfg
    try:
        _integ._sync_cfg = lambda: {}
        _user_home = os.path.normpath(os.path.expanduser("~"))
        app._export_bundle = lambda: {
            "kind": "ocleaner-custom",
            "overrides": {f"{_user_home}\\MyAppCache": {"label": f"{_user_home} 缓存", "cleanable": True, "label_kind": "clean"}},
            "fs_groups": [{"id": "g1", "name": "私有组", "paths": [f"{_user_home}\\Secret"]}],
            "units": [],
        }
        _up2 = _integ.community_upload({"scope": "all", "title": "脱敏单测"})
        _jt2 = _up2.get("json_text") or ""
        _gid_bad = False
        try:
            _integ._sync_fetch("tok", "../bad-gist")
        except ValueError:
            _gid_bad = True
        check("社区上传与同步安全：路径脱敏 %USERPROFILE% 跳过 fs_groups 且拒绝非法 gist_id",
              "%USERPROFILE%" in _jt2 and "fs_groups" not in _jt2 and _gid_bad,
              f"gid_bad={_gid_bad} jt={_jt2[:120]}")
    finally:
        app._export_bundle, _integ._sync_cfg = orig_exp2, orig_sc2

    # 9) receipts.py 畸形回执容错
    _orig_rcp_dir = app.RECEIPT_DIR
    try:
        with tempfile.TemporaryDirectory() as _td6:
            app.RECEIPT_DIR = Path(_td6)
            (app.RECEIPT_DIR / "cleanup-20261128000001.json").write_text(
                json.dumps({"time": "2026-11-28", "freed": "not-an-int", "results": "bad"}),
                encoding="utf-8")
            (app.RECEIPT_DIR / "cleanup-20261128000002.json").write_text(
                json.dumps({"time": "2026-11-28", "drive": "D", "freed": 1024, "results": []}),
                encoding="utf-8")
            _lr = app.list_receipts()
            check("回执检索：单条畸形字段回执不拖垮列表接口",
                  _lr.get("ok") and _lr.get("total") == 2, f"{_lr}")
    finally:
        app.RECEIPT_DIR = _orig_rcp_dir

    # 10) load_convert --data-dir 缺失时回退内置 8 个单位
    _orig_cp = app.CONVERT_PATH
    try:
        with tempfile.TemporaryDirectory() as _td7:
            app.CONVERT_PATH = Path(_td7) / "missing-convert.json"
            _cv = app.load_convert()
            check("换算单位回退：--data-dir 缺失 convert.json 时加载内置全部单位",
                  len(_cv.get("units") or []) >= 8, f"units={len(_cv.get('units') or [])}")
    finally:
        app.CONVERT_PATH = _orig_cp

    # 11) sys_slim_run 释放空间计量 & POST /__oc__/sys-slim 写入历史（hiber_on 不记）
    _orig_adm, _orig_pop, _orig_du = app.is_admin, app.subprocess.Popen, app.shutil.disk_usage
    try:
        class _FakeProc:
            returncode = 0
            def communicate(self, timeout=None):
                return b"ok", b""
        _du_calls = iter([type("_U", (), {"free": 1_000_000_000})(),
                          type("_U", (), {"free": 1_524_288_000})()])
        app.is_admin = lambda: True
        app.subprocess.Popen = lambda *a, **kw: _FakeProc()
        app.shutil.disk_usage = lambda *_a: next(_du_calls, type("_U", (), {"free": 1_524_288_000})())
        _sr = app.sys_slim_run("dism_cleanup")
        check("官方组件瘦身：DISM 清理按盘可用增量计量释放空间",
              _sr.get("ok") and _sr.get("freed") == 524_288_000 and _sr.get("drive"),
              f"{_sr}")
    finally:
        app.is_admin, app.subprocess.Popen, app.shutil.disk_usage = _orig_adm, _orig_pop, _orig_du

    # 12) 前端零原生 confirm/prompt/alert 残留
    _app_js = (app.BASE_DIR / "web" / "app.js").read_text(encoding="utf-8")
    _bare_dlg = app.re.findall(r"(?<![A-Za-z0-9_$.])(?:confirm|prompt|alert)\s*\(", _app_js)
    check("前端弹窗统一：web/app.js 已全部迁移至 uiConfirm/uiPrompt（无原生弹窗残留）",
          len(_bare_dlg) == 0, f"found={_bare_dlg}")

    failed = [c for c in _checks if not c[1]]
    print(f"\n==== unit_test：通过 {len(_checks) - len(failed)} 项，"
          f"失败 {len(failed)} 项 ====")
    for name, _ok, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
