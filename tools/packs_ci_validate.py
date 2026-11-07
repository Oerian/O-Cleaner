# -*- coding: utf-8 -*-
"""资源包校验与索引签名工具（仓库基建：O-Cleaner-packs 社区流水线用）。

用法：
    python tools/packs_ci_validate.py                    # 校验（CI/PR 用，出错退出 1）
    python tools/packs_ci_validate.py --update-digests   # 重算 SHA-256 并升级 index.json

校验内容：
  1. index.json 与目录内 .json 文件一一对应（缺列/多列都算错）；
  2. 每个包可解析、必填字段齐全（id/name/version/entries）、包内条目 id 唯一；
  3. 跨包冲突：条目 id 重复、match 匹配串重复（会导致路由歧义）；
  4. --update-digests：把 index.json 升级为 v2 格式（每个包附 sha256 与 version），
     供客户端在「检查资源包更新」时做完整性校验（不匹配即拒绝导入）。

纯标准库；主仓库与 O-Cleaner-packs 分发仓共用同一份脚本。
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REQUIRED_FIELDS = ("id", "name", "version", "entries")


def sha256_file(path: Path) -> str:
    hh = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hh.update(chunk)
    return hh.hexdigest()


def load_index(packs_dir: Path):
    idx_path = packs_dir / "index.json"
    if not idx_path.is_file():
        print(f"[错误] 缺少 {idx_path}")
        sys.exit(1)
    try:
        return json.loads(idx_path.read_text(encoding="utf-8")), idx_path
    except (ValueError, OSError) as exc:
        print(f"[错误] index.json 无法解析：{exc}")
        sys.exit(1)


def normalize_entries(raw):
    """index.json v1（文件名列表）与 v2（对象列表）统一成对象列表。"""
    out = []
    for it in raw or []:
        if isinstance(it, str):
            out.append({"file": it})
        elif isinstance(it, dict) and it.get("file"):
            out.append(it)
        else:
            print(f"[错误] index.json packs 里有非法条目：{it!r}")
            sys.exit(1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs-dir", default="packs")
    ap.add_argument("--update-digests", action="store_true",
                    help="重算每个包的 SHA-256 并把 index.json 升级为 v2 格式")
    args = ap.parse_args()
    packs_dir = Path(args.packs_dir)
    if not packs_dir.is_dir():
        print(f"[错误] 目录不存在：{packs_dir}")
        return 1

    errors = []
    raw_idx, idx_path = load_index(packs_dir)
    entries = normalize_entries(raw_idx.get("packs"))
    listed = {e["file"] for e in entries}
    actual = {p.name for p in packs_dir.glob("*.json") if p.name != "index.json"}

    for f in sorted(actual - listed):
        errors.append(f"{f} 在目录里但未列入 index.json")
    for f in sorted(listed - actual):
        errors.append(f"{f} 列在 index.json 里但文件不存在")

    seen_entry_ids: dict[str, str] = {}
    seen_matches: dict[str, str] = {}
    digests = {}
    for e in entries:
        fname = e["file"]
        fp = packs_dir / fname
        if not fp.is_file():
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{fname}: JSON 无法解析（{exc}）")
            continue
        for field in REQUIRED_FIELDS:
            if not data.get(field):
                errors.append(f"{fname}: 缺必填字段 {field}")
        pid = str(data.get("id") or fname)
        # 规范形哈希：对解析后的 JSON 做 sort_keys 紧凑序列化再哈希——
        # 客户端（fetch 后 json.loads）与远端文件即使格式化不同也能对上，
        # 语义相同必同哈希；任何内容改动必变哈希。
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True)
        digests[fname] = {"file": fname,
                          "sha256": hashlib.sha256(
                              canonical.encode("utf-8")).hexdigest(),
                          "version": str(data.get("version") or "")}
        ids_in_pack = set()
        for ent in data.get("entries") or []:
            eid = str(ent.get("id") or "")
            if not eid:
                errors.append(f"{fname}: 有条目缺 id")
                continue
            if eid in ids_in_pack:
                errors.append(f"{fname}: 条目 id 重复：{eid}")
            ids_in_pack.add(eid)
            if eid in seen_entry_ids and seen_entry_ids[eid] != fname:
                errors.append(f"跨包条目 id 冲突：{eid} 同时出现在 "
                              f"{seen_entry_ids[eid]} 与 {fname}")
            else:
                seen_entry_ids[eid] = fname
            match = str(ent.get("match") or "").strip().lower()
            if not match:
                errors.append(f"{fname}/{eid}: 缺 match 匹配串")
                continue
            # 冲突语义：同 match 且「判定」（cleanable/risk）一致＝释义富化
            # 的良性重述（explanations 包与规则包共存的前提）；判定相异才
            # 是路由歧义。
            verdict = (bool(ent.get("cleanable")), str(ent.get("risk") or ""))
            if match in seen_matches:
                pf, pverdict = seen_matches[match]
                if pf != fname and pverdict != verdict:
                    errors.append(f"跨包判定冲突：{match!r} 在 "
                                  f"{pf}（cleanable={pverdict[0]}, "
                                  f"risk={pverdict[1]}）与 {fname}"
                                  f"（cleanable={verdict[0]}, "
                                  f"risk={verdict[1]}）给出相反结论")
            else:
                seen_matches[match] = (fname, verdict)

    if args.update_digests:
        raw_idx["version"] = 2
        raw_idx["packs"] = [digests[e["file"]] for e in entries
                            if e["file"] in digests]
        idx_path.write_text(json.dumps(raw_idx, ensure_ascii=False, indent=1)
                            + "\n", encoding="utf-8")
        print(f"已更新 {idx_path}（v2，含 SHA-256）")

    if errors:
        print("== 资源包校验：失败 ==")
        for err in errors:
            print("  [错误] " + err)
        return 1
    print(f"== 资源包校验：通过（{len(entries)} 个包，"
          f"{len(seen_entry_ids)} 个条目，digest {'已更新' if args.update_digests else '已核对'}）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
