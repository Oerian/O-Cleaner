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

    # ---- 版本一致性 ----
    import json
    dv = json.load(open("config/drives.json", encoding="utf-8")) \
        .get("app", {}).get("version")
    check("版本一致性：app.py 与 drives.json", dv == app.APP_VERSION,
          f"app={app.APP_VERSION} drives={dv}")

    failed = [c for c in _checks if not c[1]]
    print(f"\n==== unit_test：通过 {len(_checks) - len(failed)} 项，"
          f"失败 {len(failed)} 项 ====")
    for name, _ok, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
