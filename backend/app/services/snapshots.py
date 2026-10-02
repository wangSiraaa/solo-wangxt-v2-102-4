"""场景内容快照、内容哈希与提案差异（纯函数，不依赖数据库会话）。

快照是提案版本化的“计量单位”：

- ``canonical_snapshot`` 把场景/提案内容规范化成确定结构（键排序、数值统一精度、
  载波按名排序、极化规则键规范化），再以 SHA-256 得到内容哈希；
- 哈希用于：提案对基准的乐观并发锚定、分析/规划产物与输入修订的绑定；
- ``diff_snapshots`` 输出教学上关心的四类差异：载波频率移动、掩模变化、
  极化复用规则变化、保护间隔（及泄漏限值/频段）变化。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

# 浮点统一到该精度后再哈希/比较：界面网格 1 kHz、掩模点两位小数，6 位足够稳定
HASH_DECIMALS = 6


def _round(v: float) -> float:
    return round(float(v), HASH_DECIMALS)


def _carrier_key(c: dict) -> tuple[str, int | None]:
    cid = c.get("id")
    return (str(c["name"]), int(cid) if cid is not None else -1)


def canonical_snapshot(content: dict) -> dict:
    """把 ScenarioIn 风格的内容（或既有快照）规范化为不可变快照结构。"""
    carriers = sorted(
        ({
            "name": str(c["name"]).strip(),
            "center_mhz": _round(c["center_mhz"]),
            "bandwidth_mhz": _round(c["bandwidth_mhz"]),
            "power_dbm": _round(c["power_dbm"]),
            "polarization": c["polarization"],
            "mask_name": c["mask_name"],
        } for c in content.get("carriers", [])),
        key=lambda c: c["name"],
    )
    policy = {
        policy_key(k, k.split("|")[0], k.split("|")[1]) if "|" in k else k: v
        for k, v in (content.get("reuse_policy") or {}).items()
    }
    return {
        "name": str(content.get("name", "")).strip(),
        "description": content.get("description", "") or "",
        "band_low_mhz": _round(content["band_low_mhz"]),
        "band_high_mhz": _round(content["band_high_mhz"]),
        "guard_required_mhz": _round(content["guard_required_mhz"]),
        "leakage_limit_dbm": _round(content["leakage_limit_dbm"]),
        "reuse_policy": {k: policy[k] for k in sorted(policy)},
        "carriers": carriers,
    }


def policy_key(raw: str, a: str | None = None, b: str | None = None) -> str:
    """把 "H|V" / "V|H" 之类的键规范成极化名按字母排序的形式。"""
    if a is not None and b is not None:
        return "|".join(sorted((a, b)))
    parts = raw.split("|")
    if len(parts) == 2:
        return "|".join(sorted((p.strip() for p in parts)))
    return raw


def snapshot_hash(snapshot: dict) -> str:
    """规范化 JSON（键排序、无空白、无 ASCII 转义）后取 SHA-256。"""
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def snapshot_from_scenario(sc) -> dict:
    """从 SQLAlchemy Scenario 行构造快照。"""
    return canonical_snapshot({
        "name": sc.name,
        "description": sc.description or "",
        "band_low_mhz": sc.band_low_mhz,
        "band_high_mhz": sc.band_high_mhz,
        "guard_required_mhz": sc.guard_required_mhz,
        "leakage_limit_dbm": sc.leakage_limit_dbm,
        "reuse_policy": sc.reuse_policy or {},
        "carriers": [
            {"name": c.name, "center_mhz": c.center_mhz,
             "bandwidth_mhz": c.bandwidth_mhz, "power_dbm": c.power_dbm,
             "polarization": c.polarization, "mask_name": c.mask_name}
            for c in sorted(sc.carriers, key=lambda c: c.position)
        ],
    })


def _same_num(a: float, b: float) -> bool:
    return abs(_round(a) - _round(b)) <= 5e-7


def _carrier_index(carriers: Iterable[dict]) -> dict[str, dict]:
    return {c["name"]: c for c in carriers}


def diff_snapshots(base: dict, target: dict) -> dict[str, Any]:
    """比较两个快照，输出载波/掩模/极化规则/保护间隔四类结构化差异。

    载波按名称匹配（重命名视为“删除 + 新增”，不做猜测合并）。
    """
    a = _carrier_index(base["carriers"])
    b = _carrier_index(target["carriers"])
    names_a, names_b = set(a), set(b)

    carrier_changes: list[dict] = []
    for name in sorted(names_a & names_b):
        x, y = a[name], b[name]
        changes: dict[str, dict] = {}
        if not _same_num(x["center_mhz"], y["center_mhz"]):
            changes["center_mhz"] = {"from": x["center_mhz"], "to": y["center_mhz"],
                                     "delta": _round(y["center_mhz"] - x["center_mhz"])}
        if not _same_num(x["bandwidth_mhz"], y["bandwidth_mhz"]):
            changes["bandwidth_mhz"] = {"from": x["bandwidth_mhz"],
                                        "to": y["bandwidth_mhz"]}
        if not _same_num(x["power_dbm"], y["power_dbm"]):
            changes["power_dbm"] = {"from": x["power_dbm"], "to": y["power_dbm"]}
        if x["polarization"] != y["polarization"]:
            changes["polarization"] = {"from": x["polarization"],
                                       "to": y["polarization"]}
        # 掩模变化单独成类（教学关注点）
        if x["mask_name"] != y["mask_name"]:
            changes["mask_name"] = {"from": x["mask_name"], "to": y["mask_name"]}
        if changes:
            carrier_changes.append({"carrier": name, "changes": changes})

    added = [{"carrier": n, "after": b[n]} for n in sorted(names_b - names_a)]
    removed = [{"carrier": n, "before": a[n]} for n in sorted(names_a - names_b)]

    # 掩模差异（从载波变化中抽取，便于界面聚焦“换了什么掩模”）
    mask_changes = [
        {"carrier": ch["carrier"], **ch["changes"]["mask_name"]}
        for ch in carrier_changes if "mask_name" in ch["changes"]
    ]

    # 极化复用规则差异
    pa, pb = base.get("reuse_policy") or {}, target.get("reuse_policy") or {}
    policy_changes = []
    for key in sorted(set(pa) | set(pb)):
        va, vb = pa.get(key), pb.get(key)
        if va != vb:
            policy_changes.append({"pair": key, "from": va, "to": vb})

    # 规则级差异：保护间隔为主，附带泄漏限值与可用频段
    rule_changes = []
    for field, label in (("guard_required_mhz", "保护间隔"),
                         ("leakage_limit_dbm", "掩模泄漏限值"),
                         ("band_low_mhz", "可用频段下限"),
                         ("band_high_mhz", "可用频段上限")):
        if not _same_num(base[field], target[field]):
            rule_changes.append({"field": field, "label": label,
                                 "from": base[field], "to": target[field]})

    moved = [c for c in carrier_changes if "center_mhz" in c["changes"]]
    return {
        "summary": {
            "carriers_moved": len(moved),
            "carriers_added": len(added),
            "carriers_removed": len(removed),
            "masks_changed": len(mask_changes),
            "policies_changed": len(policy_changes),
            "rules_changed": len(rule_changes),
            "total": (len(carrier_changes) + len(added) + len(removed)
                      + len(policy_changes) + len(rule_changes)),
        },
        "carrier_changes": carrier_changes,
        "carriers_added": added,
        "carriers_removed": removed,
        "mask_changes": mask_changes,
        "polarization_changes": policy_changes,
        "rule_changes": rule_changes,
    }


def diff_is_empty(diff: dict) -> bool:
    return diff["summary"]["total"] == 0


def apply_plan_to_snapshot(snapshot: dict, assignments: list[dict]) -> dict:
    """把规划结果（assignments，按载波名）合入快照，生成“采纳规划”的新内容。"""
    by_name = {a["name"]: a for a in assignments}
    content = {
        "name": snapshot["name"], "description": snapshot["description"],
        "band_low_mhz": snapshot["band_low_mhz"],
        "band_high_mhz": snapshot["band_high_mhz"],
        "guard_required_mhz": snapshot["guard_required_mhz"],
        "leakage_limit_dbm": snapshot["leakage_limit_dbm"],
        "reuse_policy": dict(snapshot["reuse_policy"]),
        "carriers": [],
    }
    for c in snapshot["carriers"]:
        a = by_name.get(c["name"])
        content["carriers"].append({
            "name": c["name"],
            "center_mhz": a["center_mhz"] if a else c["center_mhz"],
            "bandwidth_mhz": c["bandwidth_mhz"],
            "power_dbm": c["power_dbm"],
            "polarization": c["polarization"],
            "mask_name": c["mask_name"],
        })
    return canonical_snapshot(content)
