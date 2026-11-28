# -*- coding: utf-8 -*-
"""DPAPI 凭据加密（外部审计 P1-4，从 app.py 迁出）。

ai.json 的 API Key 与 sync.json 的 GitHub Token 用当前 Windows 用户的
DPAPI 凭据加密落盘（"dpapi:<base64>" 前缀），解决「绿色免安装、整个
文件夹拷走即可用」与凭据明文泄露的冲突：同机透明解密；拷到别机解密
失败安全返回空串（用户重填一次）；明文旧值原样返回，下次保存自动
升级为加密。纯标准库 ctypes，零第三方依赖。
"""

from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import os
import ctypes


def _dpapi_protect(plain: str) -> str:
    """用当前 Windows 用户的 DPAPI 凭据加密敏感串，存为 "dpapi:<base64>"。

    外部审计 P1-4：ai.json 的 API Key 与 sync.json 的 GitHub Token 此前
    明文落盘 —— 产品承诺「整个文件夹拷到另一台机器即可用」，拷去公共
    电脑时凭据随之泄露。DPAPI 绑定当前用户：同机透明解密；拷到别机
    解密失败返回空串（用户重新填一次即可），零第三方依赖。
    """
    if not plain or plain.startswith("dpapi:") or os.name != "nt":
        return plain
    try:
        import base64
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        c32 = ctypes.WinDLL("crypt32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        c32.CryptProtectData.argtypes = [
            ctypes.POINTER(_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
        c32.CryptProtectData.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p

        raw = plain.encode("utf-8")
        buf = ctypes.create_string_buffer(raw, len(raw))
        out_blob = _BLOB()
        try:
            in_blob = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
            if not c32.CryptProtectData(
                    ctypes.byref(in_blob), None, None, None, None, 0x01,
                    ctypes.byref(out_blob)):
                raise RuntimeError("Windows DPAPI 加密失败，已拒绝明文保存凭据")
            enc = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return "dpapi:" + base64.b64encode(enc).decode("ascii")
        finally:
            ctypes.memset(buf, 0, len(raw))
            if out_blob.pbData:
                k32.LocalFree(out_blob.pbData)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Windows DPAPI 加密失败，已拒绝明文保存凭据") from exc


def _dpapi_unprotect(stored: str) -> str:
    """解密 dpapi: 前缀密文；换机/换用户解密失败安全返回空串。
    明文旧值原样返回（兼容既有配置，下次保存时自动升级为加密）。"""
    if not stored or not stored.startswith("dpapi:"):
        return stored
    if os.name != "nt":
        return ""
    try:
        import base64
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        c32 = ctypes.WinDLL("crypt32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        c32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.POINTER(_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
        c32.CryptUnprotectData.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p

        raw = base64.b64decode(stored[6:])
        buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _BLOB()
        if not c32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0x01,
                ctypes.byref(out_blob)):
            return ""
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
        finally:
            if out_blob.pbData:
                if out_blob.cbData:
                    ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
                k32.LocalFree(out_blob.pbData)
    except Exception:
        return ""
