#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_package.py —— 把 O-Cleaner 打包成可拷贝到其它电脑直接运行的 zip

用法:
    python tools/make_package.py
    python tools/make_package.py --out "D:\\shared"

与旧的 make-package.ps1 相比的设计变化（2026-10-12 起）：
  * **内存打包，没有暂存目录**：直接从源码树读字节写进 zip，不再经历
    「复制到 Temp -> 规范化 -> 压缩 -> 删暂存」四步。源码树本身就是
    合规产物（CI 与 fix-encoding.ps1 守着它），复制一遍再规范化是多余的。
  * **合规校验委托 fix-encoding.ps1 -Check**：编码与换行规则只有一处
    定义（fix-encoding.ps1），本脚本打包前先对源码树跑一遍，不通过就中止。
    打包器自己不复制任何编码规则。
  * **版本号 import 自 app.APP_VERSION**：不在这里写死，升版少一处要同步。
  * **产物自校验**：zip 写完后逐条目 sha256 与源文件比对，条目数、
    内容不一致都算失败。zip 条目的时间戳固定，同内容可复现。

明确不打包：history.json / schedule.json / custom-rules.json / __pycache__，
以及运行期生成的用户数据（config/ai.json 含 API Key、config/packs-state.json、
config/packs-user/ 用户自己导入的资源包）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 分发清单。顶层目录在 zip 里固定为 O-Cleaner/。
INCLUDE = [
    "app.py",
    "ps_scripts.py",
    "启动 O-Cleaner.bat",
    "README.md",
    "docs/USAGE.md",
    "docs/CUSTOMIZE.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "HANDOFF.md",
    "config/drives.json",
    "config/targets.json",
    "config/convert.json",
    "config/filetypes.json",
    "config/kb-en.json",
    "config/game-names.json",
    "packs/index.json",
    "packs/README.md",
    "packs/windows-system.json",
    "packs/games-platform.json",
    "packs/games-titles.json",
    "packs/apps-common.json",
    "packs/explanations.json",
    "web/index.html",
    "tools/selftest.py",
    "tools/lint_rules.py",
    "tools/_lint_selftest.py",
    "tools/guard_matrix.py",
    "tools/doctor.py",
    "tools/cdp_check.js",
    "tools/fix-encoding.ps1",
    "tools/make_package.py",
    "tools/make_exe.py",
    "tools/sync_packs.py",
]

# zip 条目时间戳固定（这里用 2026-01-01），同内容 -> 同字节 -> 可复现。
ZIP_STAMP = (2026, 1, 1, 0, 0, 0)


def fail(msg: str) -> None:
    print(f"  [错误] {msg}", file=sys.stderr)
    sys.exit(1)


def preflight_encoding_check() -> None:
    """对源码树跑 fix-encoding.ps1 -Check（只读校验，不改任何文件）。"""
    fixer = HERE / "fix-encoding.ps1"
    if not fixer.exists():
        fail("缺少 tools/fix-encoding.ps1（编码规则唯一真相源）")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(fixer), "-Root", str(ROOT), "-Check"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stdout or "", file=sys.stderr)
        print(r.stderr or "", file=sys.stderr)
        fail("源码树存在编码/换行偏离，请先运行 tools/fix-encoding.ps1 修复")
    print("  编码契约预检通过（fix-encoding.ps1 -Check）")


def load_version() -> str:
    """版本号唯一真相源是 app.APP_VERSION，这里导入而不是再写一份。"""
    sys.path.insert(0, str(ROOT))
    try:
        import app  # noqa: PLC0415 —— 仅取版本常量；selftest 已证明可安全导入
    except Exception as e:  # pragma: no cover
        fail(f"无法导入 app.py 取版本号：{e}")
    ver = getattr(app, "APP_VERSION", "")
    if not re.fullmatch(r"[0-9.]+", str(ver)):
        fail(f"app.APP_VERSION 不是合法版本号：{ver!r}")
    return str(ver)


def syntax_check_app() -> None:
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    try:
        compile(src, "app.py", "exec")   # 内存编译，不落 .pyc
    except SyntaxError as e:
        fail(f"app.py 语法错误：{e}")
    print("  app.py 语法校验通过")


def build_zip(out_path: Path, ver: str) -> None:
    payloads: list[tuple[str, bytes]] = []
    for rel in INCLUDE:
        p = ROOT / rel
        if not p.exists():
            fail(f"分发清单里的文件不存在：{rel}")
        payloads.append((f"O-Cleaner/{rel}", p.read_bytes()))

    tmp = out_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in payloads:
            info = zipfile.ZipInfo(name, date_time=ZIP_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)

    # 自校验：条目数 + 逐条 sha256 与源文件一致
    with zipfile.ZipFile(tmp) as z:
        names = z.namelist()
        if names != [n for n, _ in payloads]:
            fail("zip 条目清单与分发清单不一致")
        for (name, data) in payloads:
            if hashlib.sha256(z.read(name)).digest() != hashlib.sha256(data).digest():
                fail(f"包内条目与源文件不一致：{name}")
    os.replace(tmp, out_path)

    size = out_path.stat().st_size
    print(f"\n  压缩包: {out_path}")
    print(f"  大小  : {size / 1024:,.1f} KB    条目: {len(payloads)} 个（sha256 逐条与源码一致）")
    print("\n  包含文件:")
    for name, data in payloads:
        print(f"    {name:<44} {len(data) / 1024:>8,.1f} KB")
    print("\n  在别的电脑上：解压 -> 确认已装 Python 3 -> 双击 启动 O-Cleaner.bat\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="O-Cleaner 便携分发包构建工具")
    ap.add_argument("--out", default="", help="输出目录（默认仓库根的 dist/）")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser() if args.out else ROOT / "dist"
    print()
    print("  O-Cleaner 打包（内存打包，无暂存目录）")
    print(f"  源目录: {ROOT}")
    print(f"  输出到: {out_dir}")

    preflight_encoding_check()
    syntax_check_app()
    ver = load_version()

    out_dir.mkdir(parents=True, exist_ok=True)
    build_zip(out_dir / f"O-Cleaner-v{ver}.zip", ver)


if __name__ == "__main__":
    main()
