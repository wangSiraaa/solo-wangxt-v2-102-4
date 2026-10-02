"""调频提案的快照、内容哈希与差异计算（纯函数，无数据库依赖）。

所有“输入快照”都先经过 canonicalize 归一化（键排序、浮点按 6 位小数归整），
再以 SHA-256 取哈希；分析/规划产物绑定该哈希，草稿一改哈希即变，旧规划作废。

差异覆盖教学要求的四个维度：载波频率移动、掩模、极化规则、保护间隔，
另外把频段/泄漏限值/载波增删/带宽与功率变化也一并记录（它们同样影响分析）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable

ROUND_DIGITS = 6

CARRIER_KEYS = ("name", "center_mhz", "bandwidth_mhz", "power_dbm",
                "polarization", "mask_name")
RULE_KEYS = ("guard_required_mhz", "leakage_limit_dbm", "reuse_policy")
BAND_KEYS = ("band_low_mhz", "band_high_mhz")
TOP_KEYS = ("name", "description", "band_low_mhz", "band_high_mhz",
            "guard_required_mhz", "leakage_limit_dbm", "reuse_policy",
            "carriers")


def _round_numbers(obj: Any) -> Any:
    """递归把 float 归整到 ROUND_DIGITS 位，消除 JSON/DB 往返噪声。"""
    if isinstance(obj, float):
        return round(obj, ROUND_DIGITS)
    if isinstance(obj, dict):
        return {k: _round_numbers(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_numbers(v) for v in obj]
    return obj


def canonicalize(content: dict) -> dict:
    """归整并按固定键序返回内容深拷贝（哈希与差异都以它为输入）。"""
    out: dict[str, Any] = {}
    for k in TOP_KEYS:
        if k in content:
            out[k] = copy.deepcopy(content[k])
    # 只取领域字段，忽略 id 等持久化细节
    out["carriers"] = [
        {k: c.get(k) for k in CARRIER_KEYS}
        for c in out.get("carriers", [])
    ]
    out["reuse_policy"] = dict(out.get("reuse_policy") or {})
    return _round_numbers(out)


def canonical_json(content: dict) -> str:
    return json.dumps(canonicalize(content), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def content_hash(content: dict) -> str:
    """输入快照哈希：sha256(canonical JSON)，返回 hex。"""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _carrier_index(carriers: Iterable[dict]) -> dict[str, dict]:
    return {c["name"]: c for c in carriers}


def _fmt(v: Any) -> Any:
    return v


def diff_content(base: dict, draft: dict) -> dict:
    """计算两个快照内容之间的结构化差异（base -> draft）。

    返回 {"changed": bool, "carrier_moves": [...], "carrier_changes": [...],
          "carriers_added": [...], "carriers_removed": [...],
          "mask_changes": [...], "polarization_rule_changes": [...],
          "rule_changes": {...}, "band_changes": {...}, "summary": str}
    载波以名称匹配；移动专门列出 center 变化，便于“多条频率移动”展示。
    """
    b, d = canonicalize(base), canonicalize(draft)
    bc, dc = _carrier_index(b["carriers"]), _carrier_index(d["carriers"])

    carrier_moves: list[dict] = []
    mask_changes: list[dict] = []
    carrier_changes: list[dict] = []
    for name in sorted(set(bc) & set(dc)):
        old, new = bc[name], dc[name]
        if old["center_mhz"] != new["center_mhz"]:
            carrier_moves.append({
                "carrier": name,
                "from_center_mhz": old["center_mhz"],
                "to_center_mhz": new["center_mhz"],
                "shift_mhz": round(new["center_mhz"] - old["center_mhz"],
                                   ROUND_DIGITS),
            })
        if old["mask_name"] != new["mask_name"]:
            mask_changes.append({
                "carrier": name,
                "from_mask": old["mask_name"],
                "to_mask": new["mask_name"],
            })
        other = {}
        for k in ("bandwidth_mhz", "power_dbm", "polarization"):
            if old[k] != new[k]:
                other[k] = {"from": old[k], "to": new[k]}
        if other:
            carrier_changes.append({"carrier": name, "changes": other})

    added = sorted(set(dc) - set(bc))
    removed = sorted(set(bc) - set(dc))

    # 极化复用规则：逐极化对比较（含新增、删除、修改）
    bp, dp = b.get("reuse_policy") or {}, d.get("reuse_policy") or {}
    pol_changes: list[dict] = []
    for key in sorted(set(bp) | set(dp)):
        if bp.get(key) != dp.get(key):
            pol_changes.append({"pair": key,
                                "from": bp.get(key, "（未设置，按待评估）"),
                                "to": dp.get(key, "（未设置，按待评估）")})

    rule_changes: dict[str, dict] = {}
    for k in ("guard_required_mhz", "leakage_limit_dbm"):
        if b.get(k) != d.get(k):
            rule_changes[k] = {"from": b.get(k), "to": d.get(k)}

    band_changes: dict[str, dict] = {}
    for k in BAND_KEYS:
        if b.get(k) != d.get(k):
            band_changes[k] = {"from": b.get(k), "to": d.get(k)}

    name_changed = b.get("name") != d.get("name")

    changed = bool(carrier_moves or mask_changes or carrier_changes
                   or added or removed or pol_changes or rule_changes
                   or band_changes or name_changed)

    parts = []
    if carrier_moves:
        parts.append(f"{len(carrier_moves)} 条频率移动")
    if mask_changes:
        parts.append(f"{len(mask_changes)} 处掩模变更")
    if pol_changes:
        parts.append(f"{len(pol_changes)} 条极化规则变更")
    if "guard_required_mhz" in rule_changes:
        parts.append("保护间隔 "
                     f"{rule_changes['guard_required_mhz']['from']} → "
                     f"{rule_changes['guard_required_mhz']['to']} MHz")
    if added:
        parts.append(f"新增载波 {', '.join(added)}")
    if removed:
        parts.append(f"删除载波 {', '.join(removed)}")
    if other_changes := (carrier_changes or "leakage_limit_dbm" in rule_changes
                         or band_changes or name_changed):
        tail = []
        if carrier_changes:
            tail.append("载波属性")
        if "leakage_limit_dbm" in rule_changes:
            tail.append("泄漏限值")
        if band_changes:
            tail.append("可用频段")
        if name_changed:
            tail.append("名称")
        parts.append("、".join(tail) + "变更")

    return {
        "changed": changed,
        "carrier_moves": carrier_moves,
        "carrier_changes": carrier_changes,
        "carriers_added": added,
        "carriers_removed": removed,
        "mask_changes": mask_changes,
        "polarization_rule_changes": pol_changes,
        "rule_changes": rule_changes,
        "band_changes": band_changes,
        "name_changed": name_changed,
        "summary": "；".join(parts) if parts else "与基准一致（无差异）",
    }


def moves_summary(diff: dict) -> dict:
    """差异的简单计数，供列表视图使用。"""
    return {
        "moves": len(diff.get("carrier_moves", [])),
        "mask_changes": len(diff.get("mask_changes", [])),
        "polarization_rule_changes": len(
            diff.get("polarization_rule_changes", [])),
        "guard_changed": "guard_required_mhz"
        in (diff.get("rule_changes") or {}),
        "added": len(diff.get("carriers_added", [])),
        "removed": len(diff.get("carriers_removed", [])),
    }
