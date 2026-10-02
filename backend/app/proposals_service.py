"""版本化调频提案的服务层：草稿快照、分析/规划绑定、评审、原子应用、取消、回退。

设计约束（对应教学需求）：

- 草稿永远不写基准表；基准每次变化（直接编辑 / 应用 / 回退）都追加不可变版本。
- 分析与规划产物必须绑定“输入快照哈希 + 草稿修订号”；草稿改动后旧产物失效。
- 应用用基准 current_revision + base_hash 做乐观并发校验，并在服务端重跑 post-check：
  基准被他人改动、规划过期或 post-check 不通过都整体拒绝（同一事务回滚），
  绝不部分写入频率；拒审本身作为 apply_rejected 事件单独提交保留审计。
- 取消/回退只追加版本与事件，不删除任何历史；重复请求幂等。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .assemble import bands_view, build_spectrum, to_rules, validate_masks
from .db import (Proposal, ProposalArtifact, ProposalEvent, ProposalSnapshot,
                 Scenario, ScenarioVersion)
from .schemas import ScenarioContent
from .services.analysis import Carrier, analyze
from .services.planner import BandLimits, plan
from .services.snapshots import canonicalize, content_hash, diff_content, moves_summary

# 可以从应用/回退态继续动作的提案状态
APPLIED_STATES = ("applied", "rolled_back")


class ProposalError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def not_found(kind: str, pid: int) -> ProposalError:
    return ProposalError(404, "not_found", f"{kind} {pid} 不存在")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---- 内容序列化与校验 -------------------------------------------------------

def scenario_content_dict(sc: Scenario) -> dict:
    return {
        "name": sc.name,
        "description": sc.description or "",
        "band_low_mhz": sc.band_low_mhz,
        "band_high_mhz": sc.band_high_mhz,
        "guard_required_mhz": sc.guard_required_mhz,
        "leakage_limit_dbm": sc.leakage_limit_dbm,
        "reuse_policy": dict(sc.reuse_policy or {}),
        "carriers": [
            {"name": c.name, "center_mhz": c.center_mhz,
             "bandwidth_mhz": c.bandwidth_mhz, "power_dbm": c.power_dbm,
             "polarization": c.polarization, "mask_name": c.mask_name}
            for c in sorted(sc.carriers, key=lambda c: c.position)
        ],
    }


def payload_content_dict(p: ScenarioContent) -> dict:
    return canonicalize(p.model_dump())


def validate_content(content: dict) -> dict:
    """校验草稿内容（掩模必须存在、频段合法）；返回归一化后的内容。"""
    canon = canonicalize(content)
    try:
        validate_masks([_as_carrier_in_like(c) for c in canon["carriers"]])
    except ValueError as e:
        raise ProposalError(400, "invalid_mask", str(e))
    if canon["band_high_mhz"] <= canon["band_low_mhz"]:
        raise ProposalError(400, "invalid_band",
                           "band_high_mhz 必须大于 band_low_mhz")
    return canon


class _CarrierInLike:
    """让 assemble.validate_masks 复用（它只读 mask_name）。"""
    def __init__(self, name: str, mask_name: str):
        self.name = name
        self.mask_name = mask_name


def _as_carrier_in_like(c: dict) -> _CarrierInLike:
    return _CarrierInLike(c["name"], c["mask_name"])


def _carriers_of(content: dict) -> list[Carrier]:
    return [
        Carrier(id=None, name=c["name"], center_mhz=c["center_mhz"],
                bandwidth_mhz=c["bandwidth_mhz"], power_dbm=c["power_dbm"],
                polarization=c["polarization"], mask_name=c["mask_name"])
        for c in content["carriers"]
    ]


def _rules_of(content: dict):
    from .schemas import RulesIn
    return to_rules(RulesIn(
        guard_required_mhz=content["guard_required_mhz"],
        leakage_limit_dbm=content["leakage_limit_dbm"],
        reuse_policy=content["reuse_policy"],
    ))


# ---- 基准版本链 -------------------------------------------------------------

def latest_version(s: Session, scenario_id: int) -> ScenarioVersion | None:
    return s.scalar(select(ScenarioVersion)
                    .where(ScenarioVersion.scenario_id == scenario_id)
                    .order_by(ScenarioVersion.revision.desc()))


def ensure_baseline_version(s: Session, sc: Scenario) -> ScenarioVersion:
    """老数据（建表时没有版本行）补一条 revision=0 的 baseline。"""
    v = latest_version(s, sc.id)
    if v is not None:
        return v
    content = scenario_content_dict(sc)
    v = ScenarioVersion(scenario_id=sc.id, revision=sc.current_revision,
                        kind="baseline", label="初始教学基准",
                        content=content, content_hash=content_hash(content),
                        actor="系统")
    s.add(v)
    s.flush()
    return v


def append_version(s: Session, sc: Scenario, content: dict, kind: str,
                   label: str, actor: str,
                   proposal_id: int | None) -> ScenarioVersion:
    sc.current_revision += 1
    v = ScenarioVersion(scenario_id=sc.id, revision=sc.current_revision,
                        kind=kind, label=label, content=canonicalize(content),
                        content_hash=content_hash(content),
                        proposal_id=proposal_id, actor=actor)
    s.add(v)
    s.flush()
    return v


def append_baseline_zero(s: Session, sc: Scenario, content: dict,
                         label: str = "初始教学基准",
                         actor: str = "系统") -> ScenarioVersion:
    """新场景创建时写入 revision 0 的初始基准快照。"""
    v = ScenarioVersion(scenario_id=sc.id, revision=0, kind="baseline",
                        label=label, content=canonicalize(content),
                        content_hash=content_hash(content), actor=actor)
    sc.current_revision = 0
    s.add(v)
    s.flush()
    return v


def write_scenario_content(sc: Scenario, content: dict) -> None:
    sc.name = content["name"]
    sc.description = content.get("description", "")
    sc.band_low_mhz = content["band_low_mhz"]
    sc.band_high_mhz = content["band_high_mhz"]
    sc.guard_required_mhz = content["guard_required_mhz"]
    sc.leakage_limit_dbm = content["leakage_limit_dbm"]
    sc.reuse_policy = dict(content["reuse_policy"])
    # 全量替换载波行（保持 position 顺序）
    sc.carriers = [
        _carrier_row(i, c) for i, c in enumerate(content["carriers"])
    ]


def _carrier_row(position: int, c: dict):
    from .db import CarrierRow
    return CarrierRow(
        position=position, name=c["name"], center_mhz=c["center_mhz"],
        bandwidth_mhz=c["bandwidth_mhz"], power_dbm=c["power_dbm"],
        polarization=c["polarization"], mask_name=c["mask_name"])


# ---- 审计事件 ---------------------------------------------------------------

def add_event(s: Session, proposal: Proposal, etype: str, actor: str,
              note: str = "", detail: dict | None = None) -> ProposalEvent:
    seq = (s.scalar(select(ProposalEvent.seq)
                    .where(ProposalEvent.proposal_id == proposal.id)
                    .order_by(ProposalEvent.seq.desc())) or 0) + 1
    ev = ProposalEvent(proposal_id=proposal.id, seq=seq, type=etype,
                       actor=actor, note=note or "", detail=detail or {})
    s.add(ev)
    s.flush()
    return ev


# ---- 读取与快照 -------------------------------------------------------------

def get_proposal(s: Session, proposal_id: int) -> Proposal:
    p = s.get(Proposal, proposal_id)
    if p is None:
        raise not_found("提案", proposal_id)
    return p


def current_snapshot(s: Session, proposal: Proposal) -> ProposalSnapshot:
    v = s.scalar(select(ProposalSnapshot)
                 .where(ProposalSnapshot.proposal_id == proposal.id)
                 .order_by(ProposalSnapshot.revision.desc()))
    assert v is not None  # 任何提案至少有一版快照
    return v


def add_snapshot(s: Session, proposal: Proposal, content: dict,
                 note: str) -> ProposalSnapshot:
    rev = (s.scalar(select(ProposalSnapshot.revision)
                    .where(ProposalSnapshot.proposal_id == proposal.id)
                    .order_by(ProposalSnapshot.revision.desc())) or 0) + 1
    snap = ProposalSnapshot(
        proposal_id=proposal.id, revision=rev, content=content,
        content_hash=content_hash(content),
        diff_vs_base=diff_content(proposal.base_content, content),
        note=note or "")
    s.add(snap)
    s.flush()
    return snap


def _latest_artifact(s: Session, proposal_id: int, kind: str):
    return s.scalar(select(ProposalArtifact)
                    .where(ProposalArtifact.proposal_id == proposal_id,
                           ProposalArtifact.kind == kind)
                    .order_by(ProposalArtifact.id.desc()))


def current_analysis(s: Session, proposal: Proposal):
    """只返回绑定当前修订号与当前哈希的分析产物，否则视为失效。"""
    snap = current_snapshot(s, proposal)
    a = _latest_artifact(s, proposal.id, "analysis")
    if a and a.revision == snap.revision and a.input_hash == snap.content_hash:
        return a
    return None


def current_plan(s: Session, proposal: Proposal):
    snap = current_snapshot(s, proposal)
    a = _latest_artifact(s, proposal.id, "plan")
    if a and a.revision == snap.revision and a.input_hash == snap.content_hash:
        return a
    return None


def create_proposal(s: Session, scenario_id: int, title: str, rationale: str,
                    content: dict | None, note: str, actor: str) -> Proposal:
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise not_found("场景", scenario_id)
    base_version = ensure_baseline_version(s, sc)
    base_content = canonicalize(scenario_content_dict(sc))

    if content is None:
        draft_content = dict(base_content)
    else:
        draft_content = validate_content(content)
        # 名称允许调整，但不能与其它场景冲突（应用时还会再查）
        _check_name_available(s, draft_content["name"], sc.id)

    p = Proposal(
        scenario_id=sc.id, title=title.strip(), rationale=rationale or "",
        status="draft", base_revision=base_version.revision,
        base_content=base_content, base_hash=base_version.content_hash,
        created_by=actor or "学生")
    s.add(p)
    s.flush()
    add_snapshot(s, p, draft_content, note or "从基准创建草稿")
    add_event(s, p, "created", actor or "学生",
              f"从 {sc.name} 基准 revision {base_version.revision} 创建提案",
              {"base_revision": base_version.revision,
               "base_hash": base_version.content_hash})
    s.commit()
    s.refresh(p)
    return p


def _check_name_available(s: Session, name: str, scenario_id: int) -> None:
    other = s.scalar(select(Scenario).where(
        Scenario.name == name, Scenario.id != scenario_id))
    if other is not None:
        raise ProposalError(409, "name_conflict",
                           f"场景名 {name!r} 已存在")


# ---- 草稿修订（改动即让旧分析/规划哈希失效） ---------------------------------

def update_draft(s: Session, proposal_id: int, content: dict,
                 note: str, actor: str) -> Proposal:
    p = get_proposal(s, proposal_id)
    if p.status in ("applied", "rolled_back", "cancelled"):
        raise ProposalError(
            409, f"proposal_{p.status}",
            f"提案已{_state_zh(p.status)}，草稿不可再修改")
    canon = validate_content(content)
    _check_name_available(s, canon["name"], p.scenario_id)

    snap = add_snapshot(s, p, canon, note or "草稿修订")
    if p.status == "reviewed":
        # 评审后改动必须回到草稿态，要求重新评审
        p.status = "draft"
        etype = "reset_to_draft"
        msg = f"草稿改动至 revision {snap.revision}，评审结论作废，需重新评审与规划"
    else:
        etype = "updated"
        msg = f"草稿修订至 revision {snap.revision}"
    add_event(s, p, etype, actor or "学生", msg,
              {"revision": snap.revision, "input_hash": snap.content_hash,
               "diff_summary": moves_summary(snap.diff_vs_base)})
    s.commit()
    s.refresh(p)
    return p


def _state_zh(status: str) -> str:
    return {"draft": "草稿", "reviewed": "已评审", "applied": "已应用",
            "cancelled": "已取消", "rolled_back": "已回退"}.get(status, status)


# ---- 在提案草稿上跑分析 / 规划（复用现有无状态计算服务） ----------------------

def run_analysis(s: Session, proposal_id: int, actor: str) -> Proposal:
    p = get_proposal(s, proposal_id)
    if p.status in ("applied", "rolled_back", "cancelled"):
        raise ProposalError(409, f"proposal_{p.status}",
                           f"提案已{_state_zh(p.status)}，不能再分析")
    snap = current_snapshot(s, p)
    content = snap.content
    result = analyze(_carriers_of(content), _rules_of(content))
    result["bands"] = bands_view(_carriers_of(content))
    result["spectrum"] = build_spectrum(_carriers_of(content), 0.05)
    _upsert_artifact(s, p, "analysis", snap, result, mode=None, actor=actor)
    add_event(s, p, "analysis_run", actor or "学生",
              f"对 revision {snap.revision} 运行完整分析："
              f"{result['counts']['error']} 冲突 / "
              f"{result['counts']['warning']} 警告 / "
              f"{result['counts']['pending']} 待评估",
              {"revision": snap.revision, "input_hash": snap.content_hash,
               "status": result["status"], "counts": result["counts"]})
    s.commit()
    s.refresh(p)
    return p


def run_plan(s: Session, proposal_id: int, mode: str, actor: str) -> Proposal:
    p = get_proposal(s, proposal_id)
    if p.status in ("applied", "rolled_back", "cancelled"):
        raise ProposalError(409, f"proposal_{p.status}",
                           f"提案已{_state_zh(p.status)}，不能再规划")
    snap = current_snapshot(s, p)
    content = snap.content
    carriers = _carriers_of(content)
    result = plan(carriers, _rules_of(content),
                  BandLimits(content["band_low_mhz"], content["band_high_mhz"]),
                  mode=mode)
    if result["feasible"]:
        planned = [
            Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                    bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                    polarization=a["polarization"], mask_name=a["mask_name"])
            for a in result["assignments"]
        ]
        result["post_check"] = analyze(planned, _rules_of(content))
        result["bands"] = bands_view(planned)
        result["spectrum"] = build_spectrum(planned, 0.05)
    _upsert_artifact(s, p, "plan", snap, result, mode=mode, actor=actor)
    add_event(s, p, "plan_run", actor or "学生",
              f"对 revision {snap.revision} 运行 {mode} 规划："
              + ("可行" if result["feasible"] else f"不可行（{result['status']}）"),
              {"revision": snap.revision, "input_hash": snap.content_hash,
               "mode": mode, "feasible": result["feasible"],
               "post_check_counts": result.get("post_check", {}).get("counts")})
    s.commit()
    s.refresh(p)
    return p


def _upsert_artifact(s: Session, p: Proposal, kind: str,
                     snap: ProposalSnapshot, result: dict,
                     mode: str | None, actor: str) -> ProposalArtifact:
    """同 (提案, 类型, 修订号) 覆盖；修订号变了就新增一行，保留历史。"""
    art = s.scalar(select(ProposalArtifact).where(
        ProposalArtifact.proposal_id == p.id,
        ProposalArtifact.kind == kind,
        ProposalArtifact.revision == snap.revision))
    if art is None:
        art = ProposalArtifact(proposal_id=p.id, kind=kind,
                               revision=snap.revision,
                               input_hash=snap.content_hash, mode=mode,
                               actor=actor or "学生")
        s.add(art)
    art.input_hash = snap.content_hash
    art.mode = mode
    art.actor = actor or "学生"
    art.result = result
    s.flush()
    return art


# ---- 评审 -------------------------------------------------------------------

def review_proposal(s: Session, proposal_id: int, note: str,
                    actor: str) -> Proposal:
    p = get_proposal(s, proposal_id)
    snap = current_snapshot(s, p)
    analysis = current_analysis(s, p)

    if p.status in ("applied", "rolled_back", "cancelled"):
        raise ProposalError(409, f"proposal_{p.status}",
                           f"提案已{_state_zh(p.status)}，不能评审")
    if analysis is None:
        raise ProposalError(409, "analysis_missing",
                           "草稿还没有绑定当前快照的完整分析，请先运行分析再评审")
    # 重复评审（同一修订、已评审）：幂等返回，不重复记事件
    if p.status == "reviewed":
        s.commit()
        s.refresh(p)
        return p
    p.status = "reviewed"
    add_event(s, p, "reviewed", actor or "教师", note or "评审通过",
              {"revision": snap.revision, "input_hash": snap.content_hash,
               "analysis_status": analysis.result.get("status"),
               "analysis_counts": analysis.result.get("counts")})
    s.commit()
    s.refresh(p)
    return p


# ---- 应用（乐观并发 + post-check，整体拒绝） ---------------------------------

def _pessimistic_lock(s: Session, sc: Scenario) -> None:
    """PostgreSQL 上行锁；SQLite（测试用内存库）会忽略 FOR UPDATE。"""
    if s.get_bind().dialect.name == "postgresql":
        s.scalar(select(Scenario.id).where(Scenario.id == sc.id).with_for_update())


def apply_proposal(s: Session, proposal_id: int, note: str, actor: str,
                   expected_base_revision: int | None = None) -> Proposal:
    p = get_proposal(s, proposal_id)

    # 重复应用：幂等返回当前状态
    if p.status == "applied":
        s.commit()
        s.refresh(p)
        return p
    # 已回退/已取消的提案不能再次应用（幂等地拒绝为状态冲突）
    if p.status in ("rolled_back", "cancelled"):
        raise ProposalError(409, f"proposal_{p.status}",
                           f"提案已{_state_zh(p.status)}，不能应用；请基于新版本重新提案")
    if p.status != "reviewed":
        raise ProposalError(409, "not_reviewed",
                           "提案尚未评审，必须先评审才能应用")

    sc = s.get(Scenario, p.scenario_id)
    if sc is None:
        raise not_found("场景", p.scenario_id)
    _pessimistic_lock(s, sc)
    ensure_baseline_version(s, sc)

    # 1) 乐观并发：基准版本/哈希必须与创建提案时一致
    live_content = canonicalize(scenario_content_dict(sc))
    live_hash = content_hash(live_content)
    rejection: tuple[str, str, dict] | None = None
    if sc.current_revision != p.base_revision or live_hash != p.base_hash:
        rejection = (
            "baseline_conflict",
            f"基准已被他人修改（当前 revision {sc.current_revision}，"
            f"提案基线 revision {p.base_revision}），整体拒绝应用，基准保持不变。",
            {"expected_revision": p.base_revision,
             "actual_revision": sc.current_revision,
             "expected_base_hash": p.base_hash,
             "actual_base_hash": live_hash,
             "client_expected_revision": expected_base_revision})

    snap = current_snapshot(s, p)
    plan_art = current_plan(s, p)

    # 2) 规划必须存在且绑定当前草稿快照（哈希/修订号一致）
    if rejection is None:
        if plan_art is None:
            any_plan = _latest_artifact(s, p.id, "plan")
            if any_plan is not None and (
                    any_plan.revision != snap.revision
                    or any_plan.input_hash != snap.content_hash):
                rejection = (
                    "plan_stale",
                    f"规划基于 revision {any_plan.revision} 的旧草稿，"
                    f"当前为 revision {snap.revision}，规划哈希已失效，必须重新规划",
                    {"plan_revision": any_plan.revision,
                     "plan_input_hash": any_plan.input_hash,
                     "current_revision": snap.revision,
                     "current_hash": snap.content_hash})
            else:
                rejection = ("plan_missing",
                             "提案还没有绑定当前快照的频率规划，请先规划再应用",
                             {"current_revision": snap.revision})
        elif not plan_art.result.get("feasible"):
            rejection = ("plan_infeasible",
                         f"规划不可行（{plan_art.result.get('status')}），不能应用",
                         {"mode": plan_art.mode})

    # 3) 服务端以规划结果重建目标内容并重跑完整 post-check（不信任客户端）
    post_counts = None
    target_content: dict | None = None
    if rejection is None:
        content = canonicalize(snap.content)
        assignments = {a["name"]: a["center_mhz"]
                       for a in plan_art.result["assignments"]}
        if set(assignments) != {c["name"] for c in content["carriers"]}:
            rejection = ("plan_stale",
                         "规划载波集合与当前草稿不一致，规划结果已失效，必须重新规划",
                         {"planned": sorted(assignments),
                          "current": sorted(c["name"] for c in content["carriers"])})
        else:
            target = dict(content)
            target["carriers"] = [
                {**c, "center_mhz": assignments[c["name"]]}
                for c in content["carriers"]
            ]
            target = canonicalize(target)
            post = analyze(_carriers_of(target), _rules_of(target))
            post_counts = post["counts"]
            if post_counts["error"] != 0:
                rejection = (
                    "post_check_failed",
                    f"应用前 post-check 发现 {post_counts['error']} 项冲突，"
                    "整体拒绝写入，请改用掩模感知规划或放宽约束后重新规划",
                    {"post_check_counts": post_counts,
                     "findings": [f.get("message") for f in post["findings"]
                                  if f.get("severity") == "error"],
                     "plan_mode": plan_art.mode})
            else:
                target_content = target

    if rejection is not None:
        code, message, detail = rejection
        # 拒审单独提交：即使应用事务回滚，审计也要留下
        add_event(s, p, "apply_rejected", actor or "教师", message,
                  {"reason": code, **detail})
        s.commit()
        raise ProposalError(409, code, message)

    # ---- 全部校验通过：原子写入基准 + 版本 + 应用记录 + 事件 ----
    assert target_content is not None and plan_art is not None
    write_scenario_content(sc, target_content)
    version = append_version(
        s, sc, target_content, kind="applied",
        label=f"应用提案 #{p.id} {p.title}（{plan_art.mode} 规划）",
        actor=actor or "教师", proposal_id=p.id)
    p.status = "applied"
    p.applied_revision = version.revision
    p.applied_at = datetime.now(timezone.utc)
    add_event(s, p, "applied", actor or "教师", note or
              f"应用为基准 revision {version.revision}",
              {"applied_revision": version.revision,
               "from_base_revision": p.base_revision,
               "snapshot_revision": snap.revision,
               "input_hash": snap.content_hash,
               "plan_artifact_id": plan_art.id,
               "plan_mode": plan_art.mode,
               "post_check_counts": post_counts,
               "content_hash": version.content_hash})
    s.commit()
    s.refresh(p)
    return p


# ---- 取消（幂等，仅终态化，不动基准） ---------------------------------------

def cancel_proposal(s: Session, proposal_id: int, note: str,
                    actor: str) -> Proposal:
    p = get_proposal(s, proposal_id)
    if p.status == "cancelled":
        s.commit()
        s.refresh(p)
        return p  # 幂等
    if p.status == "applied":
        raise ProposalError(409, "proposal_applied",
                           "提案已应用，不能取消；如需撤销请使用回退")
    if p.status == "rolled_back":
        raise ProposalError(409, "proposal_rolled_back",
                           "提案已回退，无需取消")
    p.status = "cancelled"
    snap = current_snapshot(s, p)
    add_event(s, p, "cancelled", actor or "教师", note or "取消提案",
              {"snapshot_revision": snap.revision,
               "input_hash": snap.content_hash})
    s.commit()
    s.refresh(p)
    return p


# ---- 回退（恢复创建时基准，生成新版本，完整保留历史） -------------------------

def rollback_proposal(s: Session, proposal_id: int, note: str,
                      actor: str) -> Proposal:
    p = get_proposal(s, proposal_id)
    if p.status == "rolled_back":
        s.commit()
        s.refresh(p)
        return p  # 幂等
    if p.status != "applied":
        raise ProposalError(
            409, f"proposal_{p.status}",
            f"提案当前为{_state_zh(p.status)}，只有已应用的提案可以回退")

    sc = s.get(Scenario, p.scenario_id)
    if sc is None:
        raise not_found("场景", p.scenario_id)
    _pessimistic_lock(s, sc)

    # 基准是否已被后续提案改变（回退目标固定是本提案创建时的基准；
    # 无论是否漂移都允许回退，但把检测结果记入审计，保证“回退依据”可解释）。
    live_hash = content_hash(canonicalize(scenario_content_dict(sc)))
    applied_version = s.scalar(select(ScenarioVersion).where(
        ScenarioVersion.scenario_id == p.scenario_id,
        ScenarioVersion.proposal_id == p.id,
        ScenarioVersion.kind == "applied"))
    drifted = applied_version is not None and live_hash != applied_version.content_hash

    restored = canonicalize(p.base_content)
    write_scenario_content(sc, restored)
    version = append_version(
        s, sc, restored, kind="rollback",
        label=f"回退提案 #{p.id} {p.title}：恢复 revision {p.base_revision} 基准",
        actor=actor or "教师", proposal_id=p.id)
    p.status = "rolled_back"
    add_event(s, p, "rolled_back", actor or "教师", note or
              f"回退至创建时基准（revision {p.base_revision}），"
              f"生成基准 revision {version.revision}",
              {"restored_base_revision": p.base_revision,
               "restored_base_hash": p.base_hash,
               "applied_revision_undone": p.applied_revision,
               "new_revision": version.revision,
               "content_hash": version.content_hash,
               "baseline_drifted_before_rollback": drifted})
    s.commit()
    s.refresh(p)
    return p


# ---- 导出（原基准 + 已应用版本 + 每次决定的依据，可离线回放） -----------------

def export_proposal(s: Session, proposal_id: int) -> dict:
    p = get_proposal(s, proposal_id)
    sc = s.get(Scenario, p.scenario_id)
    snapshots = list(s.scalars(select(ProposalSnapshot)
                               .where(ProposalSnapshot.proposal_id == p.id)
                               .order_by(ProposalSnapshot.revision)))
    artifacts = list(s.scalars(select(ProposalArtifact)
                               .where(ProposalArtifact.proposal_id == p.id)
                               .order_by(ProposalArtifact.id)))
    events = list(s.scalars(select(ProposalEvent)
                            .where(ProposalEvent.proposal_id == p.id)
                            .order_by(ProposalEvent.seq)))
    versions = [] if sc is None else list(s.scalars(
        select(ScenarioVersion).where(ScenarioVersion.scenario_id == p.scenario_id)
        .order_by(ScenarioVersion.revision)))

    def slim_artifact(a: ProposalArtifact) -> dict:
        result = dict(a.result)
        # 导出不携带绘图大数组；分析结论与规划方案完整保留
        result.pop("spectrum", None)
        result.pop("bands", None)
        return {"kind": a.kind, "revision": a.revision,
                "input_hash": a.input_hash, "mode": a.mode,
                "actor": a.actor, "created_at": _iso(a.created_at),
                "result": result,
                "is_current": (a.revision == snapshots[-1].revision
                               and a.input_hash == snapshots[-1].content_hash)}

    return {
        "proposal": {
            "id": p.id, "scenario_id": p.scenario_id, "title": p.title,
            "rationale": p.rationale, "status": p.status,
            "status_zh": _state_zh(p.status),
            "base_revision": p.base_revision, "base_hash": p.base_hash,
            "applied_revision": p.applied_revision,
            "created_by": p.created_by,
            "created_at": _iso(p.created_at), "applied_at": _iso(p.applied_at),
            "current_revision": snapshots[-1].revision,
            "current_hash": snapshots[-1].content_hash,
        },
        # 原基准：提案创建时锁定的完整快照
        "baseline_snapshot": {
            "revision": p.base_revision,
            "content_hash": p.base_hash,
            "content": canonicalize(p.base_content),
        },
        # 当前基准（应用/回退后可能已变化）
        "current_scenario": None if sc is None else {
            "id": sc.id, "name": sc.name,
            "current_revision": sc.current_revision,
            "content": canonicalize(scenario_content_dict(sc)),
            "content_hash": content_hash(scenario_content_dict(sc)),
        },
        # 每次草稿修订及其差异
        "draft_snapshots": [
            {"revision": sn.revision, "content_hash": sn.content_hash,
             "note": sn.note, "created_at": _iso(sn.created_at),
             "content": canonicalize(sn.content),
             "diff_vs_base": sn.diff_vs_base}
            for sn in snapshots
        ],
        # 每次决定的依据：分析/规划产物及其绑定的输入哈希
        "artifacts": [slim_artifact(a) for a in artifacts],
        # 可重放的决定流水
        "events": [
            {"seq": e.seq, "type": e.type, "actor": e.actor, "note": e.note,
             "detail": e.detail, "created_at": _iso(e.created_at)}
            for e in events
        ],
        # 基准版本链（原基准 / 应用版本 / 回退版本均可查看）
        "scenario_versions": [
            {"id": v.id, "revision": v.revision, "kind": v.kind,
             "label": v.label, "proposal_id": v.proposal_id,
             "actor": v.actor, "created_at": _iso(v.created_at),
             "content_hash": v.content_hash, "content": canonicalize(v.content)}
            for v in versions
        ],
        "replay": [
            f"[{_iso(e.created_at)}] {e.seq}. {e.actor} · {e.type} — {e.note}"
            for e in events
        ],
    }
