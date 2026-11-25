# -*- coding: utf-8 -*-
"""HTTP 路由层（v2.17 架构减负 Task 5，从 core.py 迁出）。

`class Handler(BaseHTTPRequestHandler)` 全部路由（/__oc__/* 的 GET /
POST / DELETE）、入口守卫（Host/Origin 双检 + CSRF 拒绝）、扩展名
缓存与封面占位图端点。app.py 经 `ocleaner.core` re-export 取得本类，
`import ocleaner.core as app` 的下游（selftest / unit_test 的
`app.Handler` monkeypatch 与断言）不受影响。

对 core 侧符号（业务函数 / 路径常量 / 会话缓存 dict）一律经 `_core.`
惰性引用 —— monkeypatch 落在 core 命名空间时对本模块调用点可见；
依赖 CONFIG_DIR / RUNTIME_DIR 的路径常量留在 core（--data-dir 重定向
时随 core 变化）。stdlib 直接导入，与 guard / games 惯例一致。
"""
from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import base64
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import ocleaner.core as _core

class Handler(BaseHTTPRequestHandler):
    server_version = f"OCleaner/{_core.APP_VERSION}"

    def log_message(self, fmt, *args):
        pass

    # ---------- CSRF 防护 ----------
    # 威胁模型：用户浏览器里开着本工具，同时又打开了一个恶意网页。
    # 那个网页可以向 http://127.0.0.1:端口 发请求（浏览器不阻止访问回环地址），
    # 于是能借用户之手清空清理历史、弹资源管理器、甚至改配置。
    # 对抗性审查实测：跨站 DELETE /history 与 POST /reveal 都返回了 200。
    # 服务端既不校验 Origin 也不发 CORS 头，浏览器因此读不到响应，
    # 但**副作用照样发生** —— 必须显式拦。
    #
    # 规则：写操作若带了 Origin/Referer 且来源不是本机，一律拒绝。
    # 用 curl 等工具不带 Origin 时仍放行（那是本机用户自己的操作）。
    def _host_ok(self) -> bool:
        """防御 DNS Rebinding：Host 头必须指向本机回环地址。

        外部审计（2026-11-05 报告 P1-1）指出：服务端此前不校验 Host，
        恶意域名可通过 DNS Rebinding 重绑定到 127.0.0.1，使攻击脚本与
        本地服务处于浏览器同源上下文 —— 写操作之外还能读全盘清单。
        """
        raw_host = (self.headers.get("Host") or "").strip().lower()
        if not raw_host:
            return False
        # 剥离端口（兼容 IPv4:port 与 [::1]:port）
        if raw_host.startswith("["):
            host = raw_host.split("]")[0] + "]"
        else:
            host = raw_host.split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _origin_ok(self) -> bool:
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if not v:
                continue
            # 外部审计（2026-11-05 报告 P1-1）：sandbox iframe / data: URI
            # 发起的跨站请求带 Origin: null，urlparse("null").hostname 为
            # None → 旧代码算出空串 "" 恰好命中白名单被放行。显式拒绝
            # "null"，且空串绝不放行。
            if v.strip().lower() == "null":
                return False
            try:
                host = (urlparse(v).hostname or "").lower()
            except Exception:
                return False
            if not host or host not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _entry_guards(self) -> bool:
        """HTTP 入口统一守卫（do_GET/do_POST/do_DELETE 顶部调用）。
        返回 True 表示已拒绝并应直接 return。"""
        if not self._host_ok():
            self._json({"error": "拒绝：Host 头不是本机回环地址"
                                 "（防 DNS Rebinding）"}, 403)
            return True
        return False

    def _deny_csrf(self) -> bool:
        """跨站写操作直接 403。返回 True 表示已拒绝。"""
        if self._origin_ok():
            return False
        self._json({"error": "拒绝跨站请求：本工具只接受从本机界面发起的写操作"}, 403)
        return True

    # ---------- 异常兜底 ----------
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception:
            self._fail()

    def _fail(self) -> None:
        print("\n[服务端错误] " + "=" * 50, file=sys.stderr, flush=True)
        traceback.print_exc()
        print("=" * 68, file=sys.stderr, flush=True)
        try:
            self._json({"error": "服务端内部错误，详见控制台窗口"}, 500)
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _game_cover(self, appid: str) -> None:
        """Steam 游戏封面/图标：从本机 Steam 客户端的 librarycache 只读取图。

        安全边界：appid 只接受纯数字（Steam AppID，最多 12 位），文件路径
        全部由服务端缓存的 Steam 根目录拼接，用户输入进不了文件系统路径，
        没有目录穿越面。找不到时 404，前端 onerror 直接隐藏图片。"""
        if not appid.isdigit() or len(appid) > 12:
            return self._json({"error": "appid 必须是数字"}, 400)
        # 「图标显示还是不正确」：竖版海报（600x900/
        # portrait）与横版 header 在 22px 方形容器里裁切后只剩画面局部，
        # 观感等同错图。策略收紧为「只回正方形图标」：named 只留 icon 类，
        # 哈希命名回退也按尺寸过滤（长宽比 > 1.2 的一律跳过）——目录里
        # 没有图标时返回 404，前端 onerror 回统一占位图，外观一致。
        for root in _core._STEAM_ROOTS_CACHE:
            base = Path(root) / "appcache" / "librarycache" / appid
            if not base.is_dir():
                continue
            named = [base / n for n in ("icon.jpg", "256p.jpg")]
            import glob as _glob
            def _icon_key(f0):
                # 哈希命名的缓存文件无名字特征——按尺寸挑：越接近正方形、
                # 面积越小的越像图标（icon），优先级高于修改时间。
                try:
                    d0 = f0.read_bytes()
                except OSError:
                    return (9, 9 * 10 ** 9)
                wh = self._img_dimensions(d0)
                if not wh:
                    return (9, 9 * 10 ** 9)
                w0, h0 = wh
                ratio = max(w0, h0) / max(1, min(w0, h0))
                return (round(ratio, 2), w0 * h0)
            anyimg = sorted(
                (Path(f0) for f0 in _glob.glob(str(base / "*"))
                 if f0.lower().endswith((".jpg", ".jpeg", ".png"))),
                key=_icon_key) if base.is_dir() else []
            for f in named + anyimg:
                if not f.is_file():
                    continue
                try:
                    body = f.read_bytes()
                except OSError:
                    continue
                wh = self._img_dimensions(body)
                if not wh:
                    continue
                w0, h0 = wh
                if max(w0, h0) / max(1, min(w0, h0)) > 1.2:
                    continue          # 非正方形（海报/header）——跳过
                ctype = "image/png" if f.name.endswith(".png") else "image/jpeg"
                return self._send(200, body, ctype)
        return self._json({"error": "no cover"}, 404)

    @staticmethod
    def _img_dimensions(data: bytes):
        """返回 (宽, 高) 或 None。PNG 读 IHDR，JPEG 扫 SOF 段——标准库实现。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                seglen = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + seglen
        return None

    def _cover_placeholder_get(self) -> None:
        """无图标游戏的占位图：用户自定义图优先，否则回内置 SVG。

        恒 200 —— 前端 onerror 链到这里，保证永远有图可显示，不会破图。"""
        try:
            data = _core.COVER_PLACEHOLDER_PATH.read_bytes()
        except OSError:
            data = b""
        mime = _core._image_mime(data) if data else ""
        if mime:
            return self._send(200, data, mime)
        return self._send(200, _core._PLACEHOLDER_SVG.encode("utf-8"),
                          "image/svg+xml")

    def _cover_placeholder_post(self, body: dict) -> None:
        """保存 / 重置默认占位图。body.data 为 dataURL（PNG/JPEG/GIF/SVG），
        body.reset 为真时删除自定义图回到内置 SVG。
        校验：解码后 ≤ 2 MB、magic bytes 认得出的图片才落盘。"""
        if body.get("reset"):
            try:
                _core.COVER_PLACEHOLDER_PATH.unlink(missing_ok=True)
            except OSError as e:
                return self._json({"error": f"重置失败：{e}"}, 500)
            return self._json({"ok": True, "reset": True})
        raw = str(body.get("data") or "")
        m = re.match(r"^data:(image/[a-z+]+);base64,(.+)$", raw, re.S)
        if not m:
            return self._json({"error": "必须是 image/* 的 dataURL"}, 400)
        try:
            data = base64.b64decode(m.group(2), validate=True)
        except Exception:                       # noqa: BLE001
            return self._json({"error": "base64 解码失败"}, 400)
        if len(data) > 2 * 1024 * 1024:
            return self._json({"error": "图片过大（上限 2 MB）"}, 400)
        mime = _core._image_mime(data)
        if not mime:
            return self._json(
                {"error": "只支持 PNG / JPEG / GIF / SVG 图片"}, 400)
        try:
            _core.COVER_PLACEHOLDER_PATH.parent.mkdir(parents=True, exist_ok=True)
            _core.COVER_PLACEHOLDER_PATH.write_bytes(data)
        except OSError as e:
            return self._json({"error": f"保存失败：{e}"}, 500)
        return self._json({"ok": True, "mime": mime, "size": len(data)})

    def _body(self) -> dict:
        """读请求体。畸形输入一律当成空对象返回，但会记下原因。

        `_json.loads` 失败 / 不是对象 / 超长，以前全部静默变成 {}，于是
        调用方只能回「没有选择任何清理项」这类牛头不对马嘴的错误 ——
        用户以为是「没勾上」，实际是请求体坏了。现在把原因记在
        `self._body_error` 里，由 do_POST 统一回 400「请求体非法」。
        """
        self._body_error = ""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._body_error = "Content-Length 非法"
            return {}
        # 限制请求体大小：本工具的请求都很小，超大的必然是异常或恶意
        if n <= 0:
            return {}
        if n > 4 * 1024 * 1024:
            self._body_error = "请求体过大（上限 4 MB）"
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._body_error = "请求体不是合法 JSON"
            return {}
        if not isinstance(data, dict):
            self._body_error = "请求体必须是 JSON 对象"
            return {}
        return data

    def _query(self) -> dict:
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        if self._entry_guards():
            return
        route = self.path.split("?")[0]
        q = self._query()
        if route in ("/", "/index.html"):
            try:
                html = (_core.WEB_DIR / "index.html").read_bytes()
            except OSError:
                html = "<h1>web/index.html 缺失</h1>".encode("utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if route == "/__oc__/state":
            return self._json(_core.apply_lang_payload(
                _core.build_state(q.get("drive"), q.get("custom"),
                            q.get("scope", "all")), q.get("lang")))
        if route == "/__oc__/progress":
            # 前端在全盘扫描期间每 0.5 秒轮询一次，用来画真实进度条
            return self._json(_core.prog_snapshot())
        if route == "/__oc__/packs":
            return self._json({"packs": _core.load_packs(use_cache=False)["packs"],
                               "remote": _core.load_packs_state().get("remote") or "",
                               "user_dir": str(_core.USER_PACKS_DIR)})
        if route == "/__oc__/ext-cache":
            letter = str(q.get("drive") or "C").rstrip(":/\\").upper()
            if not re.fullmatch(r"[A-Z]", letter):
                return self._json({"error": "drive 必须是单个盘符字母"}, 400)
            fp = _core.SNAPSHOTS_DIR / f"{letter}-ext.json"
            try:
                return self._json(json.loads(
                    fp.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                return self._json({"drive": letter, "by_ext": []})
        if route == "/__oc__/dupfiles/progress":
            return self._json(dict(_core.DUP_PROGRESS))
        if route == "/__oc__/scan":
            return self._json(_core.apply_lang_payload(
                _core.build_scan(q.get("drive"), q.get("custom"),
                           q.get("scope", "all")), q.get("lang")))
        if route == "/__oc__/history":
            return self._json(_core.load_history())
        if route == "/__oc__/receipts":
            try:
                lim = int(q.get("limit") or 50)
            except ValueError:
                return self._json({"error": "limit 必须是整数"}, 400)
            return self._json(_core.list_receipts(q.get("q") or "",
                                            q.get("drive") or "", lim))
        if route == "/__oc__/receipt":
            data, err = _core.read_receipt(q.get("name") or "")
            if err or data is None:
                return self._json({"error": err or "回执不存在"},
                                  400 if "非法" in err else 404)
            return self._json({"ok": True, "receipt": data})
        if route == "/__oc__/fsgroups":
            return self._json({"groups": _core.load_fs_groups()})
        if route == "/__oc__/engine/pref":
            # 扫描引擎偏好（前端下拉）：auto=谁快用谁（缺省 WizTree 在先）
            return self._json({"pref": _core._eng_pref()})
        if route == "/__oc__/disk-trend":
            data = _core._load_json(_core.DISK_TREND_PATH, [], use_cache=False)
            return self._json(data if isinstance(data, list) else [])
        if route == "/__oc__/trend-forecast":
            # 预计剩余可用天数（disk-trend 线性回归）+ 高频回涨目录
            return self._json(_core.trend_forecast(self._query().get("drive")))
        if route == "/__oc__/stats":
            return self._json(_core.history_stats())
        if route == "/__oc__/downloads-advice":
            # 下载目录整理建议（#35）：只读顶层，不递归不改文件
            return self._json(_core.downloads_advice())
        if route == "/__oc__/overrides":
            return self._json(_core.load_overrides())
        if route == "/__oc__/sync/status":
            # 云同步状态（不含令牌本体，只报是否已配置）
            cfg = _core._sync_cfg()
            return self._json({
                "token_set": bool(str(cfg.get("token") or "").strip()),
                "gist_id": str(cfg.get("gist_id") or ""),
                "last_push": str(cfg.get("last_push") or ""),
                "last_pull": str(cfg.get("last_pull") or ""),
                "last_pull_device": str(cfg.get("last_pull_device") or ""),
            })
        if route == "/__oc__/bundle/export":
            return self._json(_core._export_bundle())
        if route == "/__oc__/steam":
            # 60s TTL 缓存：多标签 / 横幅轮询等场景避免每次都冷启动一遍
            # PowerShell 枚举。删除游戏等「数据刚变」的场景由前端带
            # fresh=1 绕过（doClean/confirmDeleteGame 的 force 刷新）。
            fresh = q.get("fresh") == "1"
            lang = q.get("lang") or ""
            cached = _core._STEAM_RESP_CACHE.get("data")
            if (not fresh and cached is not None
                    and _core._STEAM_RESP_CACHE.get("lang") == lang
                    and time.monotonic() - _core._STEAM_RESP_CACHE["at"] < 60):
                return self._json(cached)
            data = _core.apply_lang_payload(_core.discover_steam(), lang)
            # 游戏占用趋势：清单读取成功就记一条快照（每天最多一条，
            # 体积/数量没变化不写盘）；失败不影响主返回
            if data.get("found") and data.get("total_size"):
                try:
                    _core.record_game_history(int(data["total_size"]),
                                        int(data.get("game_count") or 0))
                except Exception:                      # noqa: BLE001
                    pass
            _core._STEAM_RESP_CACHE.update(
                {"at": time.monotonic(), "data": data, "lang": lang})
            return self._json(data)
        if route == "/__oc__/game-history":
            data = _core._load_json(_core.GAME_HISTORY_PATH, [], use_cache=False)
            return self._json(data if isinstance(data, list) else [])
        if route == "/__oc__/game-cover":
            return self._game_cover(q.get("appid") or "")
        if route == "/__oc__/cover-placeholder":
            return self._cover_placeholder_get()
        if route == "/__oc__/games":
            # 全平台游戏库（Steam + Epic + GOG），steam 负载原样透传在 .steam 里
            return self._json(_core.apply_lang_payload(_core.discover_games(), q.get("lang")))
        if route == "/__oc__/game-libs":
            # 用户自定义游戏库：只读展示（体积 + 顶层子项），带 10 分钟缓存
            libs = []
            for g in _core.load_game_libs():
                try:
                    snap = _core.gamelib_snapshot(g["path"])
                except Exception as e:                     # noqa: BLE001
                    snap = {"size": 0, "size_h": "0 B", "partial": False,
                            "children": [], "error": str(e)}
                libs.append({"name": g["name"], "path": g["path"], **snap})
            return self._json({"libs": libs})
        if route == "/__oc__/partition":
            p = q.get("path") or ""
            if not p:
                return self._json({"error": "缺少 path 参数"}, 400)
            return self._json(_core.apply_lang_payload(
                _core.analyze_dir_partition(p), q.get("lang")))
        if route == "/__oc__/subdirs":
            # 大清理项逐层拆子项：查 WizTree 常驻表，任意深度都是 O(1)
            p = (q.get("path") or "").strip()
            if not p:
                return self._json({"error": "缺少 path 参数"}, 400)
            # 与 /clean 同一口径：必须是绝对路径。以前接受 "E:" 这类盘符相对
            # 路径（normpath 后变成 "E:"，isdir 为假 → 返回空列表却不报错），
            # 也接受 "C:\..\..\Windows" 这类带 .. 的迂回写法；只读接口虽无
            # 删除面，但语义含糊会让前端误以为「该目录没有子项」。
            if not os.path.isabs(p):
                return self._json({"error": "path 必须是绝对路径"}, 400)
            try:
                limit = min(400, max(1, int(q.get("limit") or 80)))
            except ValueError:
                return self._json({"error": "limit 必须是整数"}, 400)
            return self._json(_core.apply_lang_payload(
                _core.list_subdirs(q.get("drive") or "", p, limit), q.get("lang")))
        if route == "/__oc__/wiztree":
            # 新用户引导要的信息全在这里：找到没有、是不是本目录自带、怎么装
            return self._json(_core.wiztree_status())
        if route == "/__oc__/drives":
            return self._json({"drives": _core.list_drives(), "is_admin": _core.is_admin()})
        if route == "/__oc__/fullscan":
            # 旧接口名保留为别名，指向新的占用树
            letter = (q.get("drive") or "C").rstrip(":").upper()
            return self._json(_core.apply_lang_payload(
                _core.scan_disk_overview(letter), q.get("lang")))
        if route == "/__oc__/overview":
            letter = (q.get("drive") or "C").rstrip(":").upper()
            try:
                depth = int(q.get("depth") or _core.DISK_TREE_MAX_DEPTH)
            except ValueError:
                return self._json({"error": "depth 必须是整数"}, 400)
            try:
                r = _core.scan_disk_overview(letter, max(1, min(depth, 6)))
            except BaseException as e:
                # 线程级兜底：扫描线程里的异常（含 Exception 之外的类型）
                # 若直接放行，客户端只会拿到一条空响应，无从排查。
                print(f"[overview] 扫描线程异常退出：{type(e).__name__}: {e}",
                      file=sys.stderr)
                return self._json({"error": f"扫描线程异常退出"
                                            f"（{type(e).__name__}）"}, 500)
            return self._json(_core.apply_lang_payload(r, q.get("lang")))
        if route == "/__oc__/dirlevel":
            # 整棵树已在 overview 里一次扫完，这里不再单独扫一层。
            # 保留该路由但明确返回提示，避免旧前端静默失败。
            return self._json({"error": "整棵树已在 /__oc__/overview 中返回，"
                                        "无需逐层请求", "children": []}, 410)
        if route == "/__oc__/identify":
            name = q.get("name") or ""
            path = q.get("path") or name
            kind = q.get("kind") or ("dir" if os.path.isdir(path) else "file")
            return self._json(_core.identify(name, path, kind))
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):  # noqa: N802
        if self._entry_guards():
            return
        if self._deny_csrf():
            return
        route = self.path.split("?")[0]
        if route == "/__oc__/history":
            # 持锁：与 _core.append_history 的读-改-写串行化。以前这里裸写，
            # 清空瞬间若有清理任务完成，那条记录会被「清空」覆盖掉（或反过来
            # 用户以为清空了却多出一条）。
            with _core._lock, _core._cross_process_lock("history"):
                _core._atomic_write(_core.HISTORY_PATH, [])
            return self._json({"ok": True})
        if route == "/__oc__/custom-rules":
            with _core._lock:   # P2-3 补漏：清空规则同样是读-改-写（整表覆盖）
                _core.save_custom_rules([])
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self._entry_guards():
            return
        if self._deny_csrf():
            return
        route = self.path.split("?")[0]
        body = self._body()
        # 请求体坏掉就直说，别让各路由把「空对象」误判成「用户没选任何项」。
        # 所有 POST 都必须带合法 JSON 对象（少数无参路由前端本来就发 {}）。
        body_err = getattr(self, "_body_error", "")
        if body_err:
            return self._json({"error": f"请求体非法：{body_err}"}, 400)

        # ---- 自动获取 WizTree 便携版（可选加速，不装到系统里）----
        if route == "/__oc__/wiztree/install":
            if getattr(self.server, "wiz_busy", False):
                return self._json({"error": "正在下载中，请稍候"}, 409)
            self.server.wiz_busy = True
            try:
                r = _core.acquire_wiztree()
            finally:
                self.server.wiz_busy = False
            return self._json({**r, "status": _core.wiztree_status()},
                              200 if r.get("ok") else 500)

        # ---- 用户自定义游戏库（游戏占用概览）：add / remove ----
        if route == "/__oc__/game-libs":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                op = str(body.get("op") or "")
                libs = _core.load_game_libs()
                if op == "add":
                    name = str(body.get("name") or "").strip()
                    raw = str(body.get("path") or "").strip().strip('"')
                    if not name:
                        return self._json({"error": "名称不能为空"}, 400)
                    if not raw:
                        return self._json({"error": "目录不能为空"}, 400)
                    p = os.path.normpath(os.path.expandvars(
                        os.path.expanduser(raw.replace("/", "\\"))))
                    if not os.path.isabs(p):
                        return self._json({"error": "目录必须是绝对路径"
                                                   f"（收到：{raw}）"}, 400)
                    if not os.path.isdir(p):
                        return self._json({"error": f"目录不存在：{p}"}, 400)
                    if len(p) <= 3:
                        # "D:\" / "D:"：把整个盘根当游戏库既量不动也没意义
                        return self._json({"error": "不能把整个盘根当游戏库"
                                                   "（太大了，也没意义）"}, 400)
                    key = p.lower()
                    libs = [g for g in libs if g["key"] != key]   # 同目录=改名
                    libs.append({"name": name, "path": p, "key": key})
                    if len(libs) > 30:
                        return self._json({"error": "自定义游戏库最多 30 条"}, 400)
                    _core.save_game_libs(libs)
                    _core._GAMELIB_CACHE.pop(key, None)   # 改名/重加后强制重测
                elif op == "remove":
                    raw = str(body.get("path") or "").strip()
                    key = os.path.normpath(raw.replace("/", "\\")).lower()
                    rest = [g for g in libs if g["key"] != key]
                    if len(rest) == len(libs):
                        return self._json({"error": "找不到该条目"}, 404)
                    libs = rest
                    _core.save_game_libs(libs)
                    _core._GAMELIB_CACHE.pop(key, None)
                else:
                    return self._json({"error": "op 必须是 add 或 remove"}, 400)
            out = []
            for g in libs:
                try:
                    snap = _core.gamelib_snapshot(g["path"])
                except Exception as e:                     # noqa: BLE001
                    snap = {"size": 0, "size_h": "0 B", "partial": False,
                            "children": [], "error": str(e)}
                out.append({"name": g["name"], "path": g["path"], **snap})
            return self._json({"ok": True, "libs": out})

        # ---- 默认占位图（无图标游戏）：保存 / 重置 ----
        if route == "/__oc__/cover-placeholder":
            return self._cover_placeholder_post(body)

        # ---- 用户自定义聚合组（占用树的自由归组，纯显示层）----
        if route == "/__oc__/fsgroups":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                op = str(body.get("op") or "")
                groups = _core.load_fs_groups()
                if op == "add":
                    gid, name, paths = (body.get("id") or ""), (body.get("name") or ""), (body.get("paths") or [])
                    if not isinstance(paths, list) or not paths:
                        return self._json({"error": "paths 不能为空"}, 400)
                    if not str(name).strip() and not gid:
                        return self._json({"error": "组名不能为空"}, 400)
                    norm = [str(p).replace("/", "\\").rstrip("\\").strip().lower()
                            for p in paths if str(p).strip()]
                    if gid:
                        for g in groups:
                            if g["id"] == gid:
                                for p in norm:
                                    if p not in g["paths"]:
                                        g["paths"].append(p)
                                break
                        else:
                            return self._json({"error": "找不到该聚合组"}, 404)
                    else:
                        if not norm:
                            return self._json({"error": "paths 不能为空"}, 400)
                        groups.append({
                            "id": "grp-" + str(int(time.time() * 1000))[-9:],
                            "name": str(name).strip(),
                            "paths": norm,
                            "created": time.strftime("%Y-%m-%d %H:%M"),
                        })
                elif op == "remove":
                    gid, paths = body.get("id") or "", (body.get("paths") or [])
                    rest = [g for g in groups if g["id"] != gid] if not paths else groups
                    if paths:
                        drop = {str(p).replace("/", "\\").rstrip("\\").strip().lower() for p in paths}
                        for g in rest:
                            if g["id"] == gid:
                                g["paths"] = [p for p in g["paths"] if p not in drop]
                        rest = [g for g in rest if g["paths"]]
                    groups = rest
                elif op == "rename":
                    gid, name = body.get("id") or "", str(body.get("name") or "").strip()
                    if not name:
                        return self._json({"error": "组名不能为空"}, 400)
                    for g in groups:
                        if g["id"] == gid:
                            g["name"] = name
                elif op == "delete":
                    groups = [g for g in groups if g["id"] != (body.get("id") or "")]
                else:
                    return self._json({"error": f"未知操作：{op}"}, 400)
                _core.save_fs_groups(groups)
            return self._json({"ok": True, "groups": _core.load_fs_groups()})

        # ---- 执行清理 ----
        if route == "/__oc__/clean":
            drive = (body.get("drive") or "C").rstrip(":").upper()
            custom_path = body.get("custom_path") or None
            ids = set(body.get("ids") or [])
            paths_in = body.get("paths") or []
            only = body.get("only_paths") or {}
            trash = bool(body.get("trash"))
            targets = _core.build_targets(drive, custom_path)
            # 用户明确勾选了「建议保留 / 未识别」行时由前端置位（见下方 paths 段）；
            # 两个通道共用这一个开关。
            allow_nc = bool(body.get("allow_non_cleanable"))
            # 「个人数据目录」默认不清理，但用户显式勾选后可选（2026-10-21）。
            # 前端在确认弹窗里勾了「包含建议保留/未识别项」，就等价于用户
            # 亲手选了这些目录；另留一个独立开关，便于前端以后把这类目标
            # 单独做二次确认而不必连带放宽其它非可清理项。
            allow_user_data = allow_nc or bool(body.get("allow_user_data"))
            # 有效 cleanable=False 的目标（用户改成「建议保留」）默认拒绝清理，
            # 与旧版卡片勾选框禁用的行为对齐 —— 这条护栏原来只在前端。
            # 2026-10-13 起：allow_non_cleanable=True 时放行（用户在确认弹窗里
            # 明确勾选的建议保留/未识别项），系统目录保护照常生效。
            selected = [t for t in targets
                        if t["id"] in ids
                        and (t.get("cleanable") is not False or allow_nc)]

            # ---- 按路径清理（占用排行行内勾选的行）----
            # 安全边界与 /__oc__/clean-game 同一模式：**绝不信任前端说它可清理**，
            # 每条路径都由服务端重新 _core.identify()，再过 _core.check_deletable 的系统目录
            # 保护，缺一不可。
            # 2026-10-13 用户需求：建议保留 / 未识别的行也要可勾选（用户自己
            # 看过内容、明确要删的目录不该被工具拦死）。服务端因此接受
            # allow_non_cleanable=True 的请求放行非可清理路径 —— 但只放行
            # 「用户显式勾选」的这些路径，且系统目录保护与占用预检**原样生效**；
            # 前端在确认弹窗里必须如实提示「包含建议保留/未识别项」。
            skipped_paths: list[dict] = []
            for p in paths_in if isinstance(paths_in, list) else []:
                if not isinstance(p, str) or not os.path.isabs(p):
                    skipped_paths.append({"path": str(p)[:200],
                                          "reason": "拒绝：非绝对路径"})
                    continue
                p2 = os.path.normpath(p)
                if not os.path.isdir(p2):
                    skipped_paths.append({"path": p2, "reason": "目录不存在"})
                    continue
                info = _core.identify(os.path.basename(p2.rstrip("\\")) or p2, p2, "dir")
                if not info.get("cleanable") and not allow_nc:
                    skipped_paths.append(
                        {"path": p2, "reason": "服务端识别为不可清理，拒绝"})
                    continue
                # 个人数据目录要在结果里如实标注，历史记录与回执都能看出
                # 「这是用户亲手选的」，而不是工具推荐的。
                _probe_ok, _probe_why = _core.check_deletable(
                    p2, "tree", allow_user_data)
                if not _probe_ok:
                    skipped_paths.append({"path": p2, "reason": _probe_why})
                    continue
                selected.append({
                    "id": f"tree:{p2.lower()}",
                    "label": os.path.basename(p2.rstrip("\\")) or p2,
                    "path": p2, "kind": "tree",
                    "category": info.get("category", "其它"),
                    "risk": info.get("risk", "中"),
                    "cleanable": bool(info.get("cleanable")),
                })

            if not selected:
                # 报错细化：整批「目录不存在」＝扫描
                # 缓存过期（如 Steam 卸载残留目录已随卸载消失）—— 明说原
                # 因与自救动作，而不是牛头不对马嘴的「没有选择任何清理项」。
                if skipped_paths and all(
                        "不存在" in x.get("reason", "")
                        for x in skipped_paths):
                    return self._json({"error":
                        "勾选的目录已不存在（扫描缓存过期，如卸载后残留已随"
                        "之消失）——请重新扫描后再清理",
                        "skipped_paths": skipped_paths}, 400)
                return self._json({"error": "没有选择任何清理项",
                                   "skipped_paths": skipped_paths}, 400)
            # 包含关系去重：外层勾选项完整包含内层勾选项时只清最小项，
            # 不重复删除、不重复计体积（界面预估体积同样按此口径）。
            selected, merged = _core.dedup_nested_targets(selected)
            if not selected:
                return self._json({"error": "没有选择任何清理项",
                                   "skipped_paths": skipped_paths,
                                   "merged": merged}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "drive": drive,
                         "trigger": f"手动清理（{drive}:）",
                         "results": [], "freed": 0, "failed": 0,
                         "targets": len(selected)}
                for t in selected:
                    try:
                        r = _core.clean_target(t, only.get(t["id"]), drive, trash=trash,
                                         allow_user_data=allow_user_data)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        r = _core._error_result(t, drive, exc)
                    entry["results"].append(r)
                    entry["freed"] += r["freed"]
                    entry["failed"] += r["failed"]
                entry["freed_h"] = _core.human(entry["freed"])
                _core.append_history(entry)
                return self._json(_core.apply_lang_payload(
                    {"entry": entry, "disk": _core.get_disk_info(drive),
                     "stats": _core.history_stats(),
                     "skipped_paths": skipped_paths,
                     "merged": merged}, self._query().get("lang")))
            finally:
                self.server.busy = False

        # ---- 被占用文件：一键查「可能是谁占着」----
        if route == "/__oc__/locks":
            paths = body.get("paths")
            if not isinstance(paths, list):
                return self._json({"error": "paths 必须是路径数组"}, 400)
            return self._json(_core.find_lock_holders(paths))

        # ---- 大文件排行（只读全盘遍历，列出最大的单个文件）----
        if route == "/__oc__/bigfiles":
            # limit 必须是整数：传非数字时 400 而不是 ValueError 崩成 500
            # （与 GET /subdirs 对 limit 的校验同一口径）
            try:
                limit_n = int(body.get("limit") or 0)
            except (TypeError, ValueError):
                return self._json({"error": "limit 必须是整数"}, 400)
            drv0 = str(body.get("drive") or "").rstrip(":").upper()
            key0 = ("big", drv0, limit_n)
            cached0 = _core._WIZ_DIRS.get(drv0)
            hit0 = _core._BIG_CACHE.get(key0)
            if (hit0 and cached0 and hit0[0] >= cached0.get("at", 0)):
                return self._json(hit0[1])
            r0 = _core.big_files(str(body.get("drive") or ""), limit=limit_n)
            if not r0.get("error") and cached0:
                _core._BIG_CACHE[key0] = (time.time(), r0)
            return self._json(r0)

        if route == "/__oc__/dupfiles":
            # 重复大文件（#34）：基于扫描明细缓存，同体积分组 + 采样哈希。
            # 只读不删，检测本身也没有写操作，不需要像 bigfiles 那样独占锁。
            return self._json(_core.dupfiles_scan(str(body.get("drive") or "")))

        # ---- 一键清空「所选项」的回收站 ----
        # 按原始路径精确匹配回收站条目，只删配对的 $I/$R 文件，不动回收站
        # 里的其它内容（用户自己删的文件原样保留）。请求里的路径只用于
        # 匹配，真实删除永远发生在 $Recycle.Bin 内部。
        if route == "/__oc__/recycle-purge":
            paths = body.get("paths")
            if not isinstance(paths, list) or not paths:
                return self._json({"error": "paths 必须是非空路径数组"}, 400)
            if len(paths) > 200:
                return self._json({"error": "一次最多 200 个路径"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                out = _core.purge_recycle_paths(paths)
            finally:
                self.server.busy = False
            # 释放量如实入账：与常规清理同一统计通道。
            # 回收站条目可能跨盘：从各结果路径提取盘符集合，逗号连接。
            drives = ",".join(sorted({
                (r.get("path") or "")[:1].upper() for r in out["results"]
                if r.get("path")}))
            entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "drive": drives,
                     "trigger": "清空所选项的回收站",
                     "results": out["results"], "freed": out["freed"],
                     "failed": 0, "targets": len(paths),
                     "freed_h": out["freed_h"]}
            _core.append_history(entry)
            out["stats"] = _core.history_stats()
            return self._json(out)

        # ---- 删除游戏前的存档查询（只读）----
        # 在「游戏最爱放存档」的几个位置找名字与这款游戏相关的目录。
        # 启发式匹配，结果按「疑似存档」对待；前端负责把「建议先备份」讲清楚。
        if route == "/__oc__/game-saves":
            p = str(body.get("path") or "")
            if not p or not os.path.isabs(p):
                return self._json({"error": "拒绝：路径必须是绝对路径"}, 400)
            saves = _core.find_game_save_candidates(p, str(body.get("appid") or ""))
            return self._json({"saves": saves, "count": len(saves)})

        # ---- 删除单个游戏本体（Steam / Epic / GOG）----
        # 安全边界（重要）：这不是通用删除接口。路径必须**后端自己**也识别为
        # 游戏安装目录（steamapps\common 的直接子目录，或 Epic/GOG 清单里的
        # 精确安装路径），再过一遍 _core.check_deletable 的系统目录保护。
        # 缺任何一层，这个端点就会退化成「页面可以删任意目录」的口子。
        if route == "/__oc__/clean-game":
            p = str(body.get("path") or "")
            ok, why = _core.check_deletable(p, "tree")
            if not ok:
                return self._json({"error": why}, 400)
            norm = os.path.normpath(os.path.abspath(p))
            low = norm.lower()
            steam_common, _libs = _core.steam_common_dirs()
            if os.path.dirname(low) in steam_common:
                plat = "steam"
            else:
                dirs, _glibs = _core.game_install_dirs()
                plat = dirs.get(low, "")
            if not plat:
                # Epic/GOG 残留目录：与 Steam 孤儿同一信任级别 —— 必须正好是
                # 「平台安装根」的直接子目录，且此刻的平台记录里没有它。
                par = os.path.dirname(low)
                rts = _core.platform_install_roots()
                if par in rts.get("epic", set()):
                    plat = "epic"
                elif par in rts.get("gog", set()):
                    plat = "gog"
            if not plat:
                return self._json({
                    "error": "拒绝：这个目录不是已识别的游戏本体"
                             "（只允许删除 Steam / Epic / GOG 的游戏安装目录）"}, 400)
            if not os.path.isdir(norm):
                return self._json({"error": "目录不存在（可能已删除）"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                t0 = time.time()
                name = os.path.basename(norm)
                disp, _zh = _core.game_display_name(None, name)
                before = _core._size_of_dir(norm)
                # 外部审计 P2-2：与 /__oc__/clean 同款零副作用删除 ——
                # 旧实现 rm()（ignore_errors rmtree）遇到 Steam / 反作弊
                # 锁住 .exe/.dll 时会把几十 GB 资源删空只留锁死的二进制，
                # 游戏既无法运行也无法走官方卸载。先预检（命中即整体中止）
                # 再走三态删除（改名暂存 + 严格删除 + 断电恢复）。
                locked = _core._precheck_locked_files(norm)
                if locked:
                    shown = "；".join(os.path.basename(p)
                                      for p in locked[:_core._LOCK_PRECHECK_REPORT_MAX])
                    return self._json({"error": (
                        f"目标内有 {len(locked)} 个文件正被其他程序占用，"
                        f"已整体中止、未删除任何内容（请先关闭相关程序）：{shown}"
                        f"{'…' if len(locked) > _core._LOCK_PRECHECK_REPORT_MAX else ''}")}, 409)
                full, partial, note = _core.rm_tree_strict(norm)
                after = _core._size_of_dir(norm) if os.path.isdir(norm) else 0
                freed = max(0, before - after)
                if full:
                    okrm, err = True, None
                elif partial:
                    okrm, err = False, note
                else:
                    okrm, err = False, note
                letter = norm[:1].upper()
                result = {
                    "id": f"game-{abs(hash(low)) % 10**8}", "target_id": "game",
                    "label": f"游戏：{disp or name}", "category": "游戏",
                    "path": norm, "drive": letter,
                    "before": before, "after": after, "freed": freed,
                    "deleted": 1 if okrm else 0,
                    "failed": 0 if okrm else 1, "skipped": [],
                    "error": err,
                    "seconds": round(time.time() - t0, 2),
                }
                _core.append_history({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "drive": letter,
                    "trigger": f"删除游戏（{letter}:）",
                    "results": [result], "freed": freed,
                    "failed": result["failed"], "targets": 1,
                    "freed_h": _core.human(freed),
                })
                return self._json({
                    "ok": okrm, "freed": freed, "freed_h": _core.human(freed),
                    "name": disp or name, "path": norm,
                    "disk": _core.get_disk_info(letter),
                    "error": err,
                })
            finally:
                self.server.busy = False

        # ---- 定时设置 ----
        if route == "/__oc__/schedule":
            s = _core.schedule_state()
            for k in ("enabled", "interval_hours", "preset", "drive"):
                if k in body:
                    s[k] = body[k]
            # interval_hours 必须是数字：历史数据损坏或前端 bug 传入字符串时
            # 直接 400，而不是 float() 崩成 500，更不能把垃圾值原样落盘。
            # 注意校验必须在 enabled 判断**之外**：关掉定时任务时传进来的非法值
            # 一样会写盘，等下次开启时就成了下一次事故的种子（实测 enabled=false
            # 时 "abc" 被原样落盘，/state 回读到的仍是 "abc"）。
            try:
                hrs = max(0.25, float(s.get("interval_hours") or 24))
            except (TypeError, ValueError):
                return self._json(
                    {"error": "interval_hours 必须是数字"}, 400)
            s["interval_hours"] = hrs
            if s.get("enabled"):
                s["next_run"] = time.time() + hrs * 3600
            else:
                s["next_run"] = 0
            # 持锁写入：与 _run_scheduled 的锁内写回串行化，否则定时任务刚算好的
            # next_run 会被这里的陈旧快照覆盖（用户一改设置就立刻又触发一次）
            with _core._lock:
                cur = _core.schedule_state()
                cur.update({k: s[k] for k in
                            ("enabled", "interval_hours", "preset", "drive",
                             "next_run") if k in s})
                if not cur.get("enabled"):
                    cur["next_run"] = 0
                _core._atomic_write(_core.SCHEDULE_PATH, cur)
                s = cur
            return self._json(s)

        # ---- 自定义换算单位 ----
        if route == "/__oc__/units":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                cfg = _core.load_convert()
                units = list(cfg.get("units", []))
                # 逐字段校验：以前 id 传数字会 AttributeError 崩成 500，
                # bytes_per_unit 传字符串会原样写进配置，换算时再炸一次。
                uid = body.get("id")
                if not isinstance(uid, str) or not uid.strip():
                    return self._json({"error": "单位 id 必须是非空字符串"}, 400)
                uid = uid.strip()
                if len(uid) > 32 or re.search(r'[\\/:*?"<>|]', uid):
                    return self._json(
                        {"error": "单位 id 最长 32 字符，且不能包含 \\ / : * ? \" < > |"},
                        400)
                bpu = body.get("bytes_per_unit", 0)
                if bpu in (None, ""):
                    bpu = 0
                if isinstance(bpu, bool) or not isinstance(bpu, (int, float)):
                    return self._json(
                        {"error": "bytes_per_unit 必须是数字"}, 400)
                if bpu != bpu or bpu in (float("inf"), float("-inf")):
                    return self._json(
                        {"error": "bytes_per_unit 必须是有限数字"}, 400)
                if bpu < 0:
                    return self._json(
                        {"error": "bytes_per_unit 不能为负数"}, 400)

                def _txt(key: str, default: str, limit: int = 80) -> str:
                    v = body.get(key)
                    if not isinstance(v, str):
                        return default
                    v = v.strip()
                    return v[:limit] if v else default

                if not any(u.get("id") == uid for u in units):
                    label = _txt("label", uid)
                    units.append({
                        "id": uid,
                        "label": label,
                        "plural": _txt("plural", label),
                        "bytes_per_unit": bpu,
                        "note": _txt("note", "", 500),
                        "category": _txt("category", "自定义", 24),
                    })
                cfg["units"] = units
                _core._atomic_write(_core.CONVERT_PATH, cfg)
                _core._cache.pop(str(_core.CONVERT_PATH), None)
            return self._json({"units": _core.resolve_units(units), "stats": _core.history_stats()})

        if route == "/__oc__/units/delete":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                cfg = _core.load_convert()
                uid = body.get("id")
                if not isinstance(uid, str) or not uid.strip():
                    return self._json({"error": "单位 id 必须是非空字符串"}, 400)
                uid = uid.strip()
                cfg["units"] = [u for u in cfg.get("units", []) if u.get("id") != uid]
                _core._atomic_write(_core.CONVERT_PATH, cfg)
                _core._cache.pop(str(_core.CONVERT_PATH), None)
            return self._json({"units": _core.resolve_units(cfg["units"]),
                               "stats": _core.history_stats()})

        # ---- 用户自定义释义 / 结论（三条优先级的最上一条）----
        if route == "/__oc__/overrides/set":
            oid = str(body.get("id") or "").strip()
            # 键有两种形态：目标 base_id（不含斜杠），或归一化小写路径
            # （占用排行行右键自定义用的就是路径键，2026-10-13 起）。
            # 路径键只是查找键，不参与任何删除路径解析，放宽反斜杠限制；
            # 仍要求形如绝对盘符路径并限长，防止乱键写满配置文件。
            _is_path_key = bool(re.match(r"^[a-zA-Z]:\\", oid)) and len(oid) <= 500
            if (not oid
                    or len(oid) > (500 if _is_path_key else 200)
                    or ("/" in oid or ("\\" in oid and not _is_path_key))):
                return self._json({"error": "无效的清理项标识"}, 400)
            entry = {}
            for f in ("what", "rebuild", "impact", "keep_if"):
                v = body.get(f)
                if isinstance(v, str) and v.strip():
                    if len(v) > 2000:
                        return self._json({"error": "说明文本过长（上限 2000 字）"}, 400)
                    entry[f] = v.strip()
            for f in ("label", "note"):
                v = body.get(f)
                if isinstance(v, str) and v.strip():
                    if len(v) > 200:
                        return self._json({"error": "标签/说明过长（上限 200 字）"}, 400)
                    entry[f] = v.strip()
            if isinstance(body.get("cleanable"), bool):
                entry["cleanable"] = body["cleanable"]
            if isinstance(body.get("excluded"), bool):
                entry["excluded"] = body["excluded"]   # 整目录永久排除（#36）
            if isinstance(body.get("pin"), bool):
                entry["pin"] = body["pin"]
            if body.get("label_kind") in ("clean", "unk", "keep"):
                entry["label_kind"] = body["label_kind"]
            if isinstance(body.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", body["color"]):
                entry["color"] = body["color"].lower()
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                data = _core.load_overrides()
                if entry:
                    data["targets"][oid] = entry
                else:
                    data["targets"].pop(oid, None)   # 全部留空 = 恢复默认
                _core.save_overrides(data)
            return self._json({"targets": data["targets"]})

        if route == "/__oc__/overrides/delete":
            oid = str(body.get("id") or "").strip()
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                data = _core.load_overrides()
                data["targets"].pop(oid, None)
                _core.save_overrides(data)
            return self._json({"targets": data["targets"]})

        # ---- 自定义数据包：换算单位 / 释义覆盖 / 固定项规则 一次导入导出 ----
        # 导入逐项校验并报告每一项的跳过原因，所以它同时是 AI 生成结果的
        # 验收器：生成 -> 导入 -> 按报告修正 -> 再导入，循环到全部收下。
        # 落盘逻辑在模块级 _core._import_bundle（云同步 /sync/pull 复用同一套合并）。
        if route == "/__oc__/bundle/import":
            try:
                rep = _core._import_bundle(body)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, **rep})
        # ---- 云端同步：自定义数据存用户自己的 GitHub 私密 Gist ----
        if route == "/__oc__/engine/pref":
            try:
                p = _core._eng_set_pref(body.get("pref"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "pref": p})
        if route == "/__oc__/delete-paths":
            # 批量移入回收站（重复大文件 / 大文件排行 / 下载整理）：仅允许普通文件，
            # 逐个走完整护栏链 _core.check_deletable(allow_user_data=True)（系统目录、
            # 工具自身、Junction 仍硬拒绝；移入回收站可随时还原），绝不静默永久删除。
            paths_in = body.get("paths") or []
            if not isinstance(paths_in, list) or not paths_in:
                return self._json({"error": "paths 不能为空"}, 400)
            paths_in = [str(x).strip() for x in paths_in if str(x).strip()]
            if len(paths_in) > 50:
                return self._json({"error": "单次最多 50 个路径"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            trashed_total = [0]
            try:
                results = []
                for pth in paths_in:
                    item = {"path": pth, "ok": False, "trashed": False,
                            "error": None}
                    try:
                        norm = os.path.normpath(os.path.expandvars(
                            os.path.expanduser(pth.replace("/", "\\"))))
                        item["path"] = norm
                        if not os.path.isabs(norm) or not os.path.exists(norm):
                            item["error"] = "路径无效或不存在"
                        elif not os.path.isfile(norm) or os.path.islink(norm) \
                                or _core._is_reparse_dir(norm):
                            # 文件级清理专用：只允许普通文件，防整目录误移
                            item["error"] = "仅支持普通文件（目录/链接不接受）"
                        else:
                            ok, why = _core.check_deletable(norm, "files",
                                                      allow_user_data=True)
                            if not ok:
                                item["error"] = why or "护栏拦截"
                            else:
                                sz0 = os.path.getsize(norm)
                                ok2, why2 = _core.rm_trash(norm)
                                item["ok"] = ok2
                                item["trashed"] = ok2
                                if ok2:
                                    item["size"] = sz0
                                    trashed_total[0] += sz0
                                else:
                                    item["error"] = why2 or "移入回收站失败"
                    except Exception as exc:  # noqa: BLE001
                        item["error"] = f"{type(exc).__name__}: {exc}"
                    results.append(item)
                done = sum(1 for r in results if r["ok"])
                # 移入回收站不立即释放磁盘空间（同卷改名），与 _core.clean_target 保持同一口径：
                # 历史 freed 记 0，清空回收站时再入账，避免双重统计；trashed_bytes 供界面展示。
                try:
                    _core.append_history({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "trigger": "移入回收站（文件级清理）",
                        "freed": 0,
                        "freed_h": "0 B",
                        "results": [{"label": os.path.basename(r["path"]),
                                     "path": r["path"],
                                     "freed": 0,
                                     "trashed": 1 if r["trashed"] else 0,
                                     "trash_paths": [r["path"]] if r["trashed"] else [],
                                     "failed": 0 if r["ok"] else 1,
                                     "error": r.get("error")}
                                    for r in results],
                    })
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                first_err = next((r["error"] for r in results if r.get("error")), "")
                return self._json({"ok": done == len(results), "done": done,
                                   "total": len(results),
                                   "freed": 0,
                                   "trashed_bytes": trashed_total[0],
                                   "trashed_h": _core.human(trashed_total[0]),
                                   "error": "" if done == len(results) else first_err,
                                   "results": results})
            finally:
                self.server.busy = False

        if route == "/__oc__/bloatware/scan":
            now0 = time.time()
            fresh = bool(body.get("fresh"))
            if (not fresh and _core._BLOAT_CACHE["r"] is not None
                    and now0 - _core._BLOAT_CACHE["ts"] < 600):
                return self._json(_core._BLOAT_CACHE["r"])
            r0 = _core.bloatware_scan()
            if r0.get("ok"):
                _core._BLOAT_CACHE.update(ts=now0, r=r0)
            return self._json(r0)
        if route == "/__oc__/bloatware/uninstall":
            # 官方卸载程序启动：命令由服务端从注册表实时读取（客户端只传
            # reg_key），杜绝任意命令注入；reg_key 白名单限定 Uninstall 子树。
            reg_key = str(body.get("reg_key") or "")
            parsed = _core._parse_uninstall_reg_key(reg_key)
            if not parsed:
                return self._json({"error": "reg_key 非法"}, 400)
            if not _core.IS_WINDOWS:
                return self._json({"error": "仅 Windows"}, 400)
            import winreg
            hprefix, sub_path = parsed
            hive = (winreg.HKEY_LOCAL_MACHINE if hprefix == "HKLM"
                    else winreg.HKEY_CURRENT_USER)
            cmd = ""
            try:
                with winreg.OpenKey(hive, sub_path) as k:
                    try:
                        cmd = str(winreg.QueryValueEx(k, "QuietUninstallString")[0])
                    except OSError:
                        cmd = str(winreg.QueryValueEx(k, "UninstallString")[0])
            except OSError:
                pass
            if not cmd:
                return self._json({"error": "注册表里找不到卸载命令"}, 404)
            try:
                # Windows CreateProcessW 直接接受完整命令行（含引号路径与 /S /X 参数），
                # 避免 os.startfile 把整串当成文件名导致 FileNotFoundError；
                # 遇 WinError 740（需要提权）回退 os.startfile 触发 UAC。
                subprocess.Popen(cmd, shell=False)
                return self._json({"ok": True})
            except OSError as exc:
                if getattr(exc, "winerror", 0) == 740:
                    try:
                        os.startfile(cmd)
                        return self._json({"ok": True})
                    except OSError as exc2:
                        return self._json({"error": f"启动失败：{exc2}"}, 500)
                return self._json({"error": f"启动失败：{exc}"}, 500)
        if route == "/__oc__/bloatware/sweep":
            self.server.busy = getattr(self.server, "busy", False)
            if self.server.busy:
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            reg_key = str(body.get("reg_key") or "")
            dirs = body.get("dirs") or []
            if not isinstance(dirs, list) or (not dirs and not reg_key):
                return self._json({"error": "dirs/reg_key 不能同时为空"}, 400)
            self.server.busy = True
            try:
                r = _core.bloatware_sweep(reg_key, [str(x) for x in dirs][:20])
                _core._BLOAT_CACHE.update(ts=0.0, r=None)
                try:
                    _core.append_history({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "trigger": "毒瘤软件残留清扫",
                        "freed": 0,
                        "freed_h": "0 B",
                        "results": [{"label": os.path.basename(r0["path"]) or r0["path"],
                                     "path": r0["path"],
                                     "freed": 0,
                                     "trashed": 1 if r0.get("trashed") else 0,
                                     "failed": 0 if r0["ok"] else 1,
                                     "error": r0.get("error")}
                                    for r0 in r.get("results", [])],
                    })
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                if not r.get("ok"):
                    errs = [x.get("error") for x in r.get("results", []) if x.get("error")]
                    if r.get("reg_error"):
                        errs.append(r["reg_error"])
                    r["error"] = "；".join(errs[:3]) or "部分残留未能清除"
                return self._json(r)
            finally:
                self.server.busy = False
        if route == "/__oc__/junction-move":
            self.server.busy = getattr(self.server, "busy", False)
            if self.server.busy:
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                return self._json(_core.junction_move(
                    str(body.get("src") or ""),
                    str(body.get("dst") or ""),
                    merge=bool(body.get("merge")),
                    rename_conflict=bool(body.get("rename_conflict", True))))
            finally:
                self.server.busy = False
        if route == "/__oc__/junction-restore":
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                return self._json(_core.junction_restore(str(body.get("src") or "")))
            finally:
                self.server.busy = False
        if route == "/__oc__/junction/list":
            return self._json({"records": _core._load_json(_core.MOVED_JUNCTIONS_PATH, [])})
        if route == "/__oc__/recycle-restore":
            paths_in = body.get("paths") or []
            if not isinstance(paths_in, list) or not paths_in:
                return self._json({"error": "paths 不能为空"}, 400)
            if len(paths_in) > 50:
                return self._json({"error": "单次最多 50 个路径"}, 400)
            return self._json(_core.recycle_restore([str(x) for x in paths_in]))
        if route == "/__oc__/snapshot/diff":
            try:
                days_n = int(body.get("days") or 7)
            except (TypeError, ValueError):
                return self._json({"error": "days 必须是整数"}, 400)
            return self._json(_core.snapshot_diff(
                str(body.get("drive") or "C"), days_n))
        if route == "/__oc__/fs-search":
            return self._json(_core.fs_search(
                str(body.get("drive") or "C"),
                str(body.get("q") or "")))
        if route == "/__oc__/appclean/scan":
            # do_POST 作用域没有 drive 变量；不传 drive = 扫描全盘符。
            # 按 drive 分键缓存（TTL 10 分钟），传 fresh=True 绕过缓存。
            drv0 = str(body.get("drive") or "").rstrip(":").upper()
            fresh = bool(body.get("fresh"))
            now0 = time.time()
            hit = _core._APPCLEAN_CACHE.get(drv0)
            if not fresh and hit and now0 - hit[0] < 600:
                return self._json(hit[1])
            r0 = _core.appclean_scan(drv0)
            if r0.get("ok"):
                _core._APPCLEAN_CACHE[drv0] = (now0, r0)
            return self._json(r0)
        if route == "/__oc__/appclean/clean":
            paths_in = body.get("paths") or []
            if not isinstance(paths_in, list) or not paths_in:
                return self._json({"error": "paths 不能为空"}, 400)
            if len(paths_in) > 30:
                return self._json({"error": "单次最多 30 个路径"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                return self._json(_core.appclean_clean([str(x) for x in paths_in]))
            finally:
                self.server.busy = False
        if route == "/__oc__/downloads-archive":
            p0 = body.get("paths")
            only0 = [str(x) for x in p0] if isinstance(p0, list) else None
            return self._json(_core.downloads_archive(only=only0))
        if route == "/__oc__/downloads-archive/undo":
            return self._json(_core.downloads_archive_undo())
        if route == "/__oc__/sync/save":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                cfg = _core._sync_cfg()
                tok = str(body.get("token") or "").strip()
                if tok:
                    cfg["token"] = tok          # 令牌是用户显式「保存」才落盘
                if "gist_id" in body:
                    cfg["gist_id"] = str(body.get("gist_id") or "").strip()
                _core._sync_save_cfg(cfg)
            return self._json({"ok": True,
                               "token_set": bool(str(cfg.get("token") or "").strip()),
                               "gist_id": str(cfg.get("gist_id") or "")})
        if route in ("/__oc__/sync/push", "/__oc__/sync/pull",
                     "/__oc__/sync/merge"):
            token = _core._sync_token(body)
            gid = str(body.get("gist_id") or "").strip() \
                or str(_core._sync_cfg().get("gist_id") or "")
            try:
                if route.endswith("push"):
                    return self._json({"ok": True,
                                       **_core._sync_push(token, gid)})
                if route.endswith("pull"):
                    return self._json({"ok": True,
                                       **_core._sync_pull(token, gid)})
                return self._json({"ok": True, **_core._sync_two_way(token)})
            except RuntimeError as e:
                return self._json({"error": str(e)}, 502)
            except Exception as e:                     # noqa: BLE001
                return self._json({"error": f"同步失败：{type(e).__name__}: {e}"}, 502)

        # ---- 自定义规则（勾选固定项 → 命名保存 → 可套用/自动清理）----
        if route == "/__oc__/custom-rules":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                rules = _core.load_custom_rules()
                action = body.get("action")

                if action == "add" or action == "update":
                    name = (body.get("name") or "").strip()
                    ids = [str(i) for i in (body.get("ids") or [])]
                    drive = (body.get("drive") or "C").rstrip(":").upper()
                    if not name:
                        return self._json({"error": "请给规则起一个名字"}, 400)
                    if not ids:
                        return self._json({"error": "请先勾选至少一个清理项，再保存为规则"}, 400)
                    # 校验：勾选的必须是当前真实存在的目标 id
                    valid = {t["id"] for t in _core.build_targets(drive, body.get("custom_path") or None)}
                    bad = [i for i in ids if i not in valid]
                    if bad:
                        return self._json({"error": f"有 {len(bad)} 个清理项已不适用于 {drive}: 盘，"
                                                    f"请重新勾选"}, 400)
                    existing = next((r for r in rules if r["name"] == name), None)
                    if existing:
                        existing["ids"] = ids
                        existing["drive"] = drive
                        existing["auto"] = bool(body.get("auto"))
                        rule = existing
                    else:
                        rule = {"id": f"rule-{int(time.time() * 1000) % 10**10}",
                                "name": name, "ids": ids, "drive": drive,
                                "auto": bool(body.get("auto")),
                                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                        rules.append(rule)
                    _core.save_custom_rules(rules)
                    return self._json({"rules": rules, "saved": rule})

                if action == "delete":
                    rules = [r for r in rules if r.get("id") != body.get("id")]
                    _core.save_custom_rules(rules)
                    return self._json({"rules": rules})

                if action == "rename":
                    new_name = (body.get("name") or "").strip()
                    if not new_name:
                        return self._json({"error": "规则名不能为空"}, 400)
                    for r in rules:
                        if r.get("id") == body.get("id"):
                            r["name"] = new_name
                    _core.save_custom_rules(rules)
                    return self._json({"rules": rules})

                return self._json({"rules": rules})

        # ---- 执行一条自定义规则（勾选固定项的立即清理）----
        if route == "/__oc__/run-rule":
            rid = body.get("id")
            rule = next((r for r in _core.load_custom_rules() if r["id"] == rid), None)
            if not rule:
                return self._json({"error": "规则不存在"}, 404)
            drive = (rule.get("drive") or "C").rstrip(":").upper()
            custom_path = body.get("custom_path") or None
            targets = _core.build_targets(drive, custom_path)
            by_id = {t["id"]: t for t in targets}
            selected = [by_id[i] for i in rule["ids"] if i in by_id]
            if not selected:
                return self._json({"error": "规则里的清理项在当前盘上都不再适用，"
                                            "请重新勾选后覆盖保存"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "drive": drive,
                         "trigger": f"规则「{rule['name']}」（{drive}:）",
                         "rule": rule["name"],
                         "results": [], "freed": 0, "failed": 0,
                         "targets": len(selected)}
                for t in selected:
                    try:
                        r = _core.clean_target(t, None, drive)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        r = _core._error_result(t, drive, exc)
                    entry["results"].append(r)
                    entry["freed"] += r["freed"]
                    entry["failed"] += r["failed"]
                entry["freed_h"] = _core.human(entry["freed"])
                _core.append_history(entry)
                return self._json(_core.apply_lang_payload(
                    {"entry": entry, "disk": _core.get_disk_info(drive),
                     "stats": _core.history_stats(), "rule": rule["name"]},
                    self._query().get("lang")))
            finally:
                self.server.busy = False

        # ---- 在资源管理器中打开 / 定位 ----
        if route == "/__oc__/reveal":
            path = _core.expand(str(body.get("path") or ""))
            if os.path.exists(path) and _core.IS_WINDOWS:
                np = os.path.normpath(path)
                if body.get("select"):
                    # explorer /select：打开父级并高亮本项；项本身不存在时退回打开父级
                    target = np if os.path.exists(np) else os.path.dirname(np)
                    subprocess.Popen(["explorer.exe", "/select,", target],
                                     creationflags=_core.CREATE_NO_WINDOW)
                else:
                    subprocess.Popen(["explorer.exe", np],
                                     creationflags=_core.CREATE_NO_WINDOW)
                return self._json({"ok": True})
            return self._json({"error": "路径不存在"}, 404)

        # ---- 资源包 ----
        if route == "/__oc__/packs/import":
            # 三种入参：content（包本体）、path（本机文件）、url（远端地址）
            data = body.get("content")
            if data is None and body.get("path"):
                data = str(body["path"])
            if data is None and body.get("url"):
                _u = str(body["url"])
                _bad = _core._is_unsafe_remote(_u)
                if _bad:
                    return self._json({"ok": False, "error": _bad}, 400)
                data, err = _core.fetch_json(_u)
                if err:
                    return self._json({"ok": False, "error": err}, 400)
            if data is None:
                return self._json({"ok": False,
                                   "error": "需要 content / path / url 之一"}, 400)
            ok, why = _core.import_pack(data)
            res = {"ok": ok, "packs": _core.load_packs(use_cache=False)["packs"]}
            res["message" if ok else "error"] = why
            return self._json(res, 200 if ok else 400)
        if route == "/__oc__/packs/toggle":
            pid, enable = str(body.get("id") or ""), body.get("enabled")
            if not pid:
                return self._json({"ok": False, "error": "缺少 id"}, 400)
            with _core._lock:
                st = _core.load_packs_state()
                dis = set(st["disabled"])
                dis.discard(pid) if enable else dis.add(pid)
                st["disabled"] = sorted(dis)
                _core.save_packs_state(st)
            return self._json({"ok": True,
                               "packs": _core.load_packs(use_cache=False)["packs"]})
        if route == "/__oc__/packs/delete":
            pid = str(body.get("id") or "")
            f = _core.USER_PACKS_DIR / f"{os.path.basename(pid)}.json"
            if not pid or not f.is_file():
                # 内置包不可删，只能停用 —— 删了会被下次更新又拉回来
                return self._json({"ok": False,
                                   "error": "只能删除自己导入的包；内置包请用停用"},
                                  400)
            with _core._lock:
                try:
                    f.unlink()
                except OSError as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
                st = _core.load_packs_state()
                st["disabled"] = [x for x in st["disabled"] if x != pid]
                _core.save_packs_state(st)
            return self._json({"ok": True,
                               "packs": _core.load_packs(use_cache=False)["packs"]})
        if route == "/__oc__/packs/update":
            with _core._lock:  # P2-3：读-改-写串行化（防并发丢失更新）
                if body.get("remote"):
                    st = _core.load_packs_state()
                    st["remote"] = str(body["remote"]).strip()
                    _core.save_packs_state(st)
            return self._json(_core.packs_update_from_remote())

        # ---- 接入自有 AI（密钥只在服务端使用，不回传）----
        if route == "/__oc__/ai/config":
            patch = {k: v for k, v in (body or {}).items() if k in _core._AI_DEFAULTS}
            # 前端回传空密钥表示「不改」，避免每次保存都把已存的密钥抹掉。
            # 但这样一来界面上就永远清不掉 Key（用户想换供应商、或想彻底
            # 删除留存的密钥时做不到）。所以另开一个显式开关：只有明确传了
            # clear_api_key=True 才清空，空字符串仍然等于「不改」。
            if body.get("clear_api_key"):
                patch["api_key"] = ""
            elif not str(patch.get("api_key") or "").strip():
                patch.pop("api_key", None)
            with _core._lock:
                _core.save_ai_config(patch)
            return self._json({"ok": True, "ai": _core.ai_public_config()})
        if route == "/__oc__/ai/test":
            return self._json(_core.ai_test())
        if route == "/__oc__/elevate":
            # 一键提权重启（清理时遇到权限不足，不用找 bat 右键「以管理员运行」）
            return self._json(_core.elevate_relaunch())
        if route == "/__oc__/ai/models":
            return self._json(_core.ai_list_models())
        if route == "/__oc__/ai/explain":
            return self._json(_core.ai_explain(body or {}))
        if route == "/__oc__/ai/explain-batch":
            return self._json(_core.ai_explain_batch(body or {}))
        if route == "/__oc__/ai/plan":
            return self._json(_core.ai_plan(body or {}))

        # ---- 检查应用更新（只读 GitHub Releases，不自动下载） ----
        if route == "/__oc__/app/update_check":
            return self._json(_core.app_update_check())

        return self._json({"error": "not found"}, 404)
