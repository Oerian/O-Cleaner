# -*- coding: utf-8 -*-
"""外部集成（P2-1 域拆分批次 D1，从 core.py 迁出）。

三个子域：GitHub Gist 云同步（_sync_*）、自有 AI 助手接入
（load/save_ai_config、ai_chat/explain/plan）、GitHub Releases 更新检查
（app_update_check）。core.py 通过 re-import 保持裸名兼容。

对 core 的反向依赖（CONFIG_DIR / urllib 别名 / _import_bundle /
_export_bundle / _as_list 等）经 `_core.` 惰性引用 —— selftest 的
monkeypatch（core.X = stub）对本模块调用点可见。
"""
import json
import re
import socket
from datetime import datetime

import ocleaner.core as _core
from ocleaner.credentials import _dpapi_protect, _dpapi_unprotect
from ocleaner.store import _atomic_write, _load_json

def _dpapi_migrate_plaintext() -> None:
    """P1-4 收尾（批次 A3）：启动时检测到明文凭据即静默加密回写。

    DPAPI 升级此前依赖「用户下次保存」——但用户可能永不触发保存，
    换机防泄露的迁移目标应尽早生效。只重写「文件里确为明文」的凭据，
    已是 dpapi: 密文的不动（避免每次启动无谓重写文件）。
    凭据读写函数（load/save_ai_config 等）定义在本文件后段，
    所以本函数只由 main() 在全量初始化完成后调用。
    """
    try:
        _ai_raw = {}
        if _core.AI_CONFIG_PATH.exists():
            _ai_raw = json.loads(_core.AI_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(_ai_raw, dict) and str(_ai_raw.get("api_key") or "").strip() \
                and not str(_ai_raw["api_key"]).startswith("dpapi:"):
            save_ai_config(load_ai_config())   # load 解密/明文原样 → save 加密落盘
    except Exception:
        pass
    try:
        _sy_raw = {}
        if _core._SYNC_CFG_PATH.exists():
            _sy_raw = json.loads(_core._SYNC_CFG_PATH.read_text(encoding="utf-8"))
        if isinstance(_sy_raw, dict) and str(_sy_raw.get("token") or "").strip() \
                and not str(_sy_raw["token"]).startswith("dpapi:"):
            _cfg = _sync_cfg()
            _cfg["token"] = _sy_raw["token"]   # 明文原样传入，保存时加密
            _sync_save_cfg(_cfg)
    except Exception:
        pass

_GIST_API = "https://api.github.com/gists"
_GIST_FILE = "oc-cleaner-sync.json"


def _sync_cfg() -> dict:
    try:
        d = json.loads(_core._SYNC_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(d, dict) and d.get("token"):
        # P1-4：DPAPI 密文透明解密；换机解密失败 → 空 token（需重填）
        d["token"] = _dpapi_unprotect(str(d["token"]))
    return d if isinstance(d, dict) else {}


def _sync_save_cfg(cfg: dict) -> None:
    try:
        _core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if cfg.get("token"):
            # P1-4：令牌 DPAPI 加密落盘，明文不出现在任何文件里
            cfg["token"] = _dpapi_protect(str(cfg["token"]))
        _atomic_write(_core._SYNC_CFG_PATH, cfg)
    except OSError:
        pass


def _sync_token(body: dict | None = None) -> str:
    """令牌优先取请求体（前端本次输入），否则回落本机保存的配置。"""
    t = str((body or {}).get("token") or "").strip()
    return t or str(_sync_cfg().get("token") or "").strip()


def _gist_request(method: str, url: str, token: str,
                  payload: dict | None = None,
                  timeout: int = 30) -> tuple[int, dict]:
    """Gist API 薄封装，返回 (status, json)。HTTP 错误抛 RuntimeError
    （带 GitHub 的 message），网络错误原样抛由调用方兜底。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = _core._UrlRequest(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "O-Cleaner",
        "Content-Type": "application/json"})
    try:
        with _core._urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except _core._HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            detail = ""
        if e.code == 401:
            raise RuntimeError("令牌无效或已过期（GitHub 401）——"
                               "请到 github.com/settings/tokens 重新生成一个带 gist 权限的令牌")
        if e.code == 403 and "rate limit" in detail.lower():
            raise RuntimeError("GitHub API 限流（403），稍后再试")
        raise RuntimeError(f"GitHub API 错误 {e.code}：{detail or e.reason}")


def _sync_push(token: str, gist_id: str = "") -> dict:
    """把本机自定义数据全集推到私密 Gist；gist_id 为空时自动创建。"""
    if not token:
        raise RuntimeError("缺少访问令牌：先在下方填入 GitHub 个人访问令牌并保存")
    bundle = _core._export_bundle()
    payload = {"kind": "ocleaner-sync",
               "device": socket.gethostname(),
               "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "data": bundle}
    body = {"description": "O-Cleaner 自定义数据同步（自动生成，请勿手工编辑）",
            "public": False,
            "files": {_GIST_FILE: {"content": json.dumps(
                payload, ensure_ascii=False, indent=1)}}}
    if gist_id:
        st, r = _gist_request("PATCH", f"{_GIST_API}/{gist_id}", token, body)
    else:
        st, r = _gist_request("POST", _GIST_API, token, body)
    if st not in (200, 201) or not r.get("id"):
        raise RuntimeError(f"Gist 保存失败（{st}）")
    gid = str(r["id"])
    cfg = _sync_cfg()
    if gid != gist_id:
        cfg["gist_id"] = gid            # 首次推送：记住自动创建的 Gist
    cfg["last_push"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _sync_save_cfg(cfg)
    return {"gist_id": gid, "html_url": str(r.get("html_url") or "")}


def _sync_fetch(token: str, gist_id: str) -> dict:
    """取回 Gist 里的同步载荷；远端为空/未建返回 {}。"""
    if not gist_id:
        return {}
    st, r = _gist_request("GET", f"{_GIST_API}/{gist_id}", token)
    if st != 200:
        raise RuntimeError(f"Gist 读取失败（{st}）")
    f = (r.get("files") or {}).get(_GIST_FILE) or {}
    content = str(f.get("content") or "")
    if f.get("truncated") and f.get("raw_url"):
        # Gist 单文件超过 1MB 会截断：走 raw_url 拿全文（纯文本响应）
        req = _core._UrlRequest(f["raw_url"], headers={
            "Authorization": f"Bearer {token}", "User-Agent": "O-Cleaner"})
        with _core._urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
    if not content.strip():
        return {}
    try:
        payload = json.loads(content)
    except ValueError:
        raise RuntimeError("远端内容不是有效 JSON —— Gist 可能被手工改坏，"
                           "推一次「双向同步」即可用本机数据重建")
    if not isinstance(payload, dict):
        raise RuntimeError("远端内容格式不对（应为对象）")
    return payload


def _sync_pull(token: str, gist_id: str) -> dict:
    """拉取远端并按 bundle/import 的逐项校验合并进本地（远端并集进本地）。"""
    payload = _sync_fetch(token, gist_id)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data:
        return {"empty": True, "device": str(payload.get("device") or "")}
    rep = _core._import_bundle(data)
    cfg = _sync_cfg()
    cfg["last_pull"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg["last_pull_device"] = str(payload.get("device") or "")
    _sync_save_cfg(cfg)
    return {"empty": False, "device": str(payload.get("device") or ""), **rep}


def _sync_two_way(token: str) -> dict:
    """双向同步：拉取合并进本地 → 把合并后的全集推回远端。
    结果是两端并集；本地已有的项不会被远端覆盖，远端独有的项进本地。"""
    cfg = _sync_cfg()
    pull_rep = _sync_pull(token, str(cfg.get("gist_id") or ""))
    push_rep = _sync_push(token, str(_sync_cfg().get("gist_id") or ""))
    return {"pull": pull_rep, "push": push_rep}

_AI_DEFAULTS = {
    "enabled": False, "base_url": "", "api_key": "", "model": "",
    "temperature": 0.2, "timeout": 60, "extra_prompt": "",
}


def load_ai_config() -> dict:
    d = _load_json(_core.AI_CONFIG_PATH, {})
    cfg = dict(_AI_DEFAULTS)
    if isinstance(d, dict):
        for k in _AI_DEFAULTS:
            if d.get(k) is not None:
                cfg[k] = d[k]
    # P1-4：落盘的是 DPAPI 密文，读取时透明解密；明文旧值原样返回
    cfg["api_key"] = _dpapi_unprotect(str(cfg.get("api_key") or ""))
    return cfg


def save_ai_config(patch: dict) -> dict:
    cfg = load_ai_config()
    for k in _AI_DEFAULTS:
        if k in patch:
            cfg[k] = patch[k]
    cfg["enabled"] = bool(cfg.get("enabled"))
    try:
        cfg["temperature"] = max(0.0, min(2.0, float(cfg.get("temperature") or 0.2)))
    except (TypeError, ValueError):
        cfg["temperature"] = 0.2
    try:
        cfg["timeout"] = max(5, min(300, int(cfg.get("timeout") or 60)))
    except (TypeError, ValueError):
        cfg["timeout"] = 60
    cfg["api_key"] = _dpapi_protect(str(cfg.get("api_key") or ""))
    _atomic_write(_core.AI_CONFIG_PATH, cfg)
    return cfg


def ai_public_config() -> dict:
    """给前端看的配置：密钥只回传「有没有」和尾 4 位，绝不回传全文。"""
    cfg = load_ai_config()
    key = str(cfg.get("api_key") or "")
    return {
        "enabled": bool(cfg.get("enabled")),
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "temperature": cfg.get("temperature"),
        "timeout": cfg.get("timeout"),
        "has_key": bool(key),
        "key_tail": key[-4:] if len(key) >= 4 else "",
        "ready": bool(cfg.get("enabled") and cfg.get("base_url") and cfg.get("model")),
    }


def ai_list_models() -> dict:
    """拉取接口的可用模型列表（OpenAI 兼容 GET /models）。

    DeepSeek（https://api.deepseek.com/v1）等主流兼容服务都提供该端点；
    Ollama 的 /v1/models 也兼容。识别不到标准结构时如实报错，
    绝不编造模型名 —— 用户仍可在输入框手填。
    """
    cfg = load_ai_config()
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        return {"ok": False,
                "error": "请先填写接口地址（http:// 或 https:// 开头）再获取模型列表"}
    headers = {"User-Agent": _core.APP_UA}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        req = _core._UrlRequest(base + "/models", headers=headers)
        with _core._urlopen(req, timeout=min(30, int(cfg.get("timeout") or 60))) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": f"获取失败：{e}"}
    ids: list[str] = []
    if isinstance(data, dict):
        for m in (data.get("data") or []):
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
        if not ids and isinstance(data.get("models"), list):
            # 兼容把模型列表放在 models 字段下的服务
            ids = [str(m.get("name") or m.get("model") or m.get("id") or "")
                   for m in data["models"] if isinstance(m, dict)]
    elif isinstance(data, list):
        ids = [str(m) for m in data if isinstance(m, str)]
    ids = sorted(set(i for i in ids if i))
    if not ids:
        return {"ok": False,
                "error": "接口没有返回可识别的模型列表（不是 OpenAI 兼容的 /models？），仍可手动填写模型名"}
    return {"ok": True, "models": ids}


_APP_REPO = "Oerian/O-Cleaner"


def _version_tuple(v: str) -> tuple[int, ...] | None:
    """把 "2.6" / "v2.7.1" 之类的版本串解析成可比较的整数元组；解析不出返回 None。"""
    parts = re.findall(r"\d+", str(v))
    return tuple(int(p) for p in parts) if parts else None


def app_update_check() -> dict:
    """从 GitHub Releases 读取最新版本并与 APP_VERSION 比较。

    只读不改：是否下载、是否升级永远由用户自己点；网络不可达时如实报错，
    绝不静默当作「已是最新」。版本比较按数字逐段比，无法解析时如实说明。
    """
    url = f"https://api.github.com/repos/{_APP_REPO}/releases/latest"
    try:
        req = _core._UrlRequest(url, headers={"User-Agent": _core.APP_UA})
        with _core._urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "current": APP_VERSION,
                "error": f"无法连接 GitHub（{e}）；也可以直接到仓库 Releases 页查看"}
    tag = str(data.get("tag_name") or "").lstrip("vV")
    latest = _version_tuple(tag)
    cur = _version_tuple(APP_VERSION)
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    download = next((a.get("browser_download_url") for a in assets
                     if str(a.get("name") or "").lower().endswith(".zip")), "")
    return {"ok": True, "current": APP_VERSION, "latest": tag or None,
            "newer": bool(latest and cur and latest > cur),
            "parse_note": None if (latest and cur) else "版本号无法解析，请自行到发布页核对",
            "name": data.get("name") or "",
            "url": data.get("html_url") or "",
            "download": download,
            "published": (data.get("published_at") or "")[:10]}

def ai_chat(messages: list[dict], timeout: int | None = None) -> tuple[bool, str]:
    """调用用户自己的 Chat Completions 接口。返回 (成功, 文本或错误)。"""
    cfg = load_ai_config()
    if not cfg.get("enabled"):
        return False, "AI 未启用：在右侧「AI 助手」面板填好接口并勾选「启用」"
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        return False, "接口地址必须以 http:// 或 https:// 开头"
    if not str(cfg.get("model") or "").strip():
        return False, "未填写模型名"
    body = json.dumps({
        "model": cfg["model"], "messages": messages,
        "temperature": float(cfg.get("temperature") or 0.2), "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "User-Agent": _core.APP_UA}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        req = _core._UrlRequest(base + "/chat/completions", data=body,
                          method="POST", headers=headers)
        with _core._urlopen(req, timeout=timeout or int(cfg.get("timeout") or 60)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, f"调用失败：{e}"
    try:
        return True, str(data["choices"][0]["message"]["content"])
    except Exception:
        return False, "接口返回的结构不是 Chat Completions：" + str(data)[:200]


def ai_test() -> dict:
    ok, txt = ai_chat([{"role": "user",
                        "content": "只回复两个字：可用"}], timeout=30)
    return {"ok": ok, "reply": txt if ok else "", "error": "" if ok else txt,
            "config": ai_public_config()}


AI_EXPLAIN_SYS = (
    "你是磁盘清理工具里的说明撰写者。用户会给你一个磁盘上的目录或文件，"
    "以及本工具已有的识别结果。请用简体中文给出简洁、准确、不夸大的说明，"
    "分成四段，每段一行，以「是什么：」「能否重建：」「删除影响：」「建议保留的情况：」开头。"
    "不要用感叹号，不要用「彻底」「永久」「务必」这类词，不要编造你不知道的事实；"
    "不确定就写「不确定」。全文不超过 200 字。"
)


def ai_explain(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    path = str(payload.get("path") or "").strip()
    if not name and not path:
        return {"ok": False, "error": "缺少 name 或 path"}
    kind = str(payload.get("kind") or "dir")
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    local = "\n".join([
        f"名称：{name}", f"完整路径：{path}",
        f"类型：{'目录' if kind == 'dir' else '文件'}",
        f"占用：{payload.get('size_h') or '未知'}",
        f"本工具的内置识别：{info.get('title') or '未识别'}"
        f"（分类 {info.get('category') or '未知'}）",
        f"内置说明：{info.get('what') or '无'}",
    ])
    msgs = [{"role": "system", "content": AI_EXPLAIN_SYS},
            {"role": "user", "content": local}]
    ok, txt = ai_chat(msgs)
    return {"ok": ok, "text": txt if ok else "", "error": "" if ok else txt}


AI_PLAN_SYS = (
    "你是磁盘清理工具的方案助手。用户会给你一个清理目标和一份候选清单，"
    "清单里的每一项都已经由本工具的规则判定为「可清理」。"
    "你只能从清单里挑选，不得添加清单以外的任何路径或文件。"
    "请只输出一个 JSON 对象，不要输出任何其它文字，格式为："
    '{"summary":"一到两句话的说明","picks":[{"id":"清单里的 id","reason":"为什么选它"}]}'
    "。如果清单为空，picks 就返回空数组，summary 说明原因。"
)


def ai_plan(payload: dict) -> dict:
    request = str(payload.get("request") or "").strip()
    if not request:
        return {"ok": False, "error": "请先描述你的清理目标"}
    raw = payload.get("candidates")
    cands = [c for c in _core._as_list(raw) if isinstance(c, dict) and c.get("id")]
    # 只把「内置判定可清理」的项交给模型，从源头上杜绝模型建议删不该删的东西
    cands = [c for c in cands if c.get("cleanable") is True][:120]
    lines = [f"- id={c['id']} | {c.get('label') or ''} | {c.get('size_h') or ''}"
             f" | {c.get('category') or ''} | 路径 {c.get('path') or ''}"
             for c in cands]
    msg = (f"清理目标：{request}\n\n候选清单（{len(cands)} 项）：\n"
           + ("\n".join(lines) if lines else "（空）"))
    ok, txt = ai_chat([{"role": "system", "content": AI_PLAN_SYS},
                       {"role": "user", "content": msg}])
    if not ok:
        return {"ok": False, "error": txt}
    parsed = _extract_json(txt)
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "模型没有返回可解析的 JSON",
                "raw": txt[:500]}
    known = {c["id"]: c for c in cands}
    picks, dropped = [], []
    for p in _core._as_list(parsed.get("picks")):
        pid = str((p or {}).get("id") or "")
        if pid in known:
            picks.append({"id": pid, "reason": str((p or {}).get("reason") or ""),
                          "label": known[pid].get("label") or pid,
                          "size_h": known[pid].get("size_h") or ""})
        elif pid:
            dropped.append(pid)      # 模型编了清单外的 id：丢掉并如实回报
    return {"ok": True, "summary": str(parsed.get("summary") or ""),
            "picks": picks, "dropped": dropped, "candidate_count": len(cands),
            "raw": txt[:2000] if not picks else ""}


def _extract_json(text: str):
    """从模型回复里抠出第一个 JSON 对象。模型常把 JSON 包在 ```json 里。"""
    s = str(text or "").strip()
    if "```" in s:
        parts = s.split("```")
        for seg in parts:
            seg = seg.strip()
            if seg.lower().startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{") or seg.startswith("["):
                s = seg
                break
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = s.find(op), s.rfind(cl)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    try:
        return json.loads(s)
    except Exception:
        return None
