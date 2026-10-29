# -*- coding: utf-8 -*-
"""构建免 Python 分发包：内嵌 python.org 官方 embeddable 运行时。

设计（与仓库零依赖取向一致，不用 PyInstaller）：
- 运行时用 python.org 官方 embeddable zip（无安装器、无 pip、无第三方依赖），
  解包进 O-Cleaner/python/，并给 ._pth 加一行 ".." 把项目根挂进 sys.path。
- 项目文件复用 make_package.INCLUDE 分发清单（清单唯一真相源，两工具共用）。
- 启动器换成免检测版：直接 python\\python.exe app.py（app.py 自己拉起浏览器），
  新增 README-RUN.txt 说明便携使用与 SmartScreen 提示。
- zip 条目时间戳固定（与 make_package 同一枚 2026-01-01），同内容 -> 同字节
  -> 可复现；写完逐条 sha256 自校验。
- 运行时缓存：build/ 下的 embeddable zip 只下载一次（build/ 已 gitignore）。

如实说明：不做代码签名，Windows SmartScreen 首次运行会提示，
「更多信息 -> 仍要运行」即可；这是无签名分发的正常现象。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from make_package import (  # noqa: E402 —— 复用分发清单与预检，规则只写一处
    INCLUDE,
    ZIP_STAMP,
    fail,
    load_version,
    preflight_encoding_check,
)

# 运行时版本写死并显式声明：换版本要实际跑一遍自测再改这里。
EMBED_VERSION = "3.13.7"
EMBED_URL = (f"https://www.python.org/ftp/python/{EMBED_VERSION}/"
             f"python-{EMBED_VERSION}-embed-amd64.zip")
BUILD_DIR = ROOT / "build"
EMBED_CACHE = BUILD_DIR / Path(EMBED_URL).name
PY_PREFIX = "O-Cleaner/python/"

# 免检测启动器：纯 ASCII + CRLF（.bat 编码契约）；app.py 启动后自己拉起浏览器。
EMBEDDED_BAT = (
    "@echo off\r\n"
    "rem O-Cleaner embedded launcher - no system Python needed.\r\n"
    "rem The embedded runtime lives in the python folder next to this file.\r\n"
    "start \"\" \"%~dp0python\\python.exe\" \"%~dp0app.py\"\r\n"
)

# 免安装版说明（UTF-8 BOM + CRLF，记事本直接可读）。
README_RUN = """\ufeffO-Cleaner 免安装版（内嵌 Python 运行时）
========================================

启动：双击 启动 O-Cleaner.bat，浏览器会自动打开界面。

本包自带官方 Python 运行时（python/ 目录），不需要在这台电脑上安装
或配置 Python；也请不要把 python/ 目录单独挪走或精简。

便携说明：程序的历史记录、配置等数据都写在本文件夹内（config/ 等），
把整个文件夹放到任意可写目录即可使用；不要放在 Program Files 这类
需要管理员权限的位置。

首次运行提示：本程序未做代码签名，Windows SmartScreen 可能弹出
「已保护你的电脑」。点「更多信息」->「仍要运行」即可，这是无签名
绿色软件的正常现象。

卸载：删除本文件夹即可，不留注册表项、不写系统目录。
""".replace("\n", "\r\n")


def ensure_embed_zip() -> Path:
    """拿一份官方 embeddable zip：有缓存用缓存，没有就下载并验明正身。"""
    if EMBED_CACHE.exists():
        print(f"  运行时缓存命中: {EMBED_CACHE.name}")
        return EMBED_CACHE
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  下载官方 embeddable 运行时: {EMBED_URL}")

    def _progress(blocks: int, bs: int, total: int) -> None:
        if total > 0 and blocks % 200 == 0:
            print(f"    {min(blocks * bs, total) / 1024 / 1024:,.1f} / "
                  f"{total / 1024 / 1024:,.1f} MB")

    tmp = EMBED_CACHE.with_suffix(".zip.part")
    try:
        urllib.request.urlretrieve(EMBED_URL, tmp, _progress)
    except Exception as e:  # noqa: BLE001
        fail(f"下载失败：{e}\n  可手动下载后放到 {EMBED_CACHE}")
    os.replace(tmp, EMBED_CACHE)
    return EMBED_CACHE


def embed_runtime_payloads() -> list[tuple[str, bytes]]:
    """从 embeddable zip 展开出全部条目（内存中，不落盘），改写 ._pth。

    ._pth 加一行 ".."，让 O-Cleaner/ 项目根（app.py、config/、packs/、web/）
    进入 sys.path；这是「python/ 子目录 + 项目根在上一层」布局的关键。
    """
    src = ensure_embed_zip()
    out: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if "python.exe" not in names:
            fail("embeddable 包里没有 python.exe（下载不完整或版本变了）")
        pth_names = [n for n in names if n.endswith("._pth")]
        if len(pth_names) != 1:
            fail(f"embeddable 包里 ._pth 数量异常：{pth_names}")
        pth_name = pth_names[0]
        for n in names:
            data = z.read(n)
            if n == pth_name:
                text = data.decode("utf-8")
                if ".." not in text.splitlines():
                    lines = text.splitlines()
                    # 在 "." 行后插入 ".."（项目根在 python/ 的上一层）
                    dot = lines.index(".") if "." in lines else len(lines) - 1
                    lines.insert(dot + 1, "..")
                    text = "\n".join(lines) + "\n"
                data = text.encode("utf-8")
            out.append((PY_PREFIX + n, data))
    return out


def build_embed_zip(out_path: Path, ver: str) -> None:
    payloads: list[tuple[str, bytes]] = []
    for rel in INCLUDE:
        if rel == "启动 O-Cleaner.bat":
            continue                     # 免安装版换成免检测启动器
        p = ROOT / rel
        if not p.exists():
            fail(f"分发清单里的文件不存在：{rel}")
        payloads.append((f"O-Cleaner/{rel}", p.read_bytes()))
    payloads.append(("O-Cleaner/启动 O-Cleaner.bat", EMBEDDED_BAT.encode("ascii")))
    payloads.append(("O-Cleaner/README-RUN.txt", README_RUN.encode("utf-8")))
    payloads.extend(embed_runtime_payloads())

    tmp = out_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in payloads:
            info = zipfile.ZipInfo(name, date_time=ZIP_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)

    # 自校验：条目清单一致 + 逐条 sha256 复核（项目文件对源码、运行时对官方包）
    with zipfile.ZipFile(tmp) as z:
        if z.namelist() != [n for n, _ in payloads]:
            fail("zip 条目清单与构建清单不一致")
        for name, data in payloads:
            if hashlib.sha256(z.read(name)).digest() != hashlib.sha256(data).digest():
                fail(f"包内条目与构建负载不一致：{name}")
    os.replace(tmp, out_path)

    n_proj = sum(1 for n, _ in payloads if not n.startswith(PY_PREFIX))
    n_rt = len(payloads) - n_proj
    size = out_path.stat().st_size
    print(f"\n  压缩包: {out_path}")
    print(f"  大小  : {size / 1024 / 1024:,.1f} MB"
          f"    项目文件 {n_proj} 个 + 内嵌运行时 {n_rt} 个"
          f"（逐条 sha256 自校验通过）")
    print("\n  使用：解压 -> 双击 启动 O-Cleaner.bat（无需安装 Python）")
    print("  提示：未做代码签名，SmartScreen 首次运行选「更多信息 -> 仍要运行」。\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="O-Cleaner 免安装版构建工具（内嵌官方运行时）")
    ap.add_argument("--out", default="", help="输出目录（默认仓库根的 dist/）")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser() if args.out else ROOT / "dist"
    print()
    print("  O-Cleaner 免安装版打包（内嵌 embeddable 运行时，内存打包）")
    print(f"  源目录: {ROOT}")
    print(f"  输出到: {out_dir}")
    print(f"  运行时: python-{EMBED_VERSION}-embed-amd64（python.org 官方包）")

    preflight_encoding_check()
    ver = load_version()

    out_dir.mkdir(parents=True, exist_ok=True)
    build_embed_zip(out_dir / f"O-Cleaner-v{ver}-win64-embed.zip", ver)


if __name__ == "__main__":
    main()
