"""版本化调频提案工作流。

状态机::

    draft ──review──▶ reviewed ──apply──▶ applied ──rollback──▶ rolled_back
      ▲                  │                   （重复 apply 幂等返回）
      └──── reopen ──────┘
    draft/reviewed ──cancel──▶ cancelled（重复 cancel 幂等返回）

关键不变量：

1. 提案创建时锚定基准（``base_version`` + ``base_snapshot_hash``）；
   应用以基准版本做乐观并发校验——基准版本已被他人推进时整体拒绝，频率不做部分写入。
2. 分析/规划产物绑定“输入快照哈希 + 修订号”；草稿产生新修订后，旧修订上的
   规划结果立即过期（``plan_expired``），必须重新规划才能应用。
3. 应用在单事务内完成：条件更新场景版本 → 替换载波 → 写版本快照 → 翻状态 → 审计事件。
   post-check（对提案目标内容重跑完整分析）失败时事务回滚，场景保持不变。
4. 取消/回退/重复应用均幂等；所有决定（含被拒绝的应用尝试）都留下审计事件。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import (CarrierRow, FmProposal, FmProposalArtifact, FmProposalEvent,
                  FmProposalRevision, Scenario, ScenarioVersion)
from .analysis import AnalysisRules, Carrier, analyze
from .planner import BandLimits, plan
from .snapshots import (apply_plan_to_snapshot, canonical_snapshot,
                        diff_snapshots, snapshot_from_scenario, snapshot_hash)

DRAFT, REVIEWED, APPLIED, CANCELLED, ROLLED_BACK = (
    "draft", "reviewed", "applied", "cancelled", "rolled_back")

# 允许的状态迁移（目标状态 -> 允许的来源集合）
TRANSITIONS: dict[str, set[str]] = {
    REVIEWED: {DRAFT},
    DRAFT: {REVIEWED},          # reopen
    APPLIED: {REVIEWED},
    CANCELLED: {DRAFT, REVIEWED},
    ROLLED_BACK: {APPLIED},
}
TERMINAL_STATUSES = {CANCELLED, ROLLED_BACK}


class ProposalError(Exception):
    """带机器可读 code 的工作流错误，由 API 层映射为 HTTP 状态码。"""

    def __init__(self, status_code: int, code: str, message: str,
                 extra: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


# ---- 快照与内容 -------------------------------------------------------------


def _content_from_snapshot(snap: dict) -> dict:
    """快照 → ScenarioIn 风格内容 dict。"""
    return {
        "name": snap["name"], "description": snap.get("description", ""),
        "band_low_mhz": snap["band_low_mhz"],
        "band_high_mhz": snap["band_high_mhz"],
        "guard_required_mhz": snap["guard_required_mhz"],
        "leakage_limit_dbm": snap["leakage_limit_dbm"],
        "reuse_policy": dict(snap["reuse_policy"]),
        "carriers": [dict(c) for c in snap["carriers"]],
    }


def _rules_of(snap: dict) -> AnalysisRules:
    return AnalysisRules(
        guard_required_mhz=snap["guard_required_mhz"],
        leakage_limit_dbm=snap["leakage_limit_dbm"],
        reuse_policy=dict(snap["reuse_policy"]),
    )


def _carriers_of(snap: dict) -> list[Carrier]:
    return [Carrier(id=None, name=c["name"], center_mhz=c["center_mhz"],
                    bandwidth_mhz=c["bandwidth_mhz"], power_dbm=c["power_dbm"],
                    polarization=c["polarization"], mask_name=c["mask_name"])
            for c in snap["carriers"]]


def snapshot_baseline(session: Session, scenario_id: int) -> tuple[Scenario, dict, str]:
    sc = session.get(Scenario, scenario_id)
    if sc is None:
        raise ProposalError(404, "not_found", f"场景 {scenario_id} 不存在")
    snap = snapshot_from_scenario(sc)
    return sc, snap, snapshot_hash(snap)


def _get_proposal(session: Session, proposal_id: int) -> FmProposal:
    p = session.get(FmProposal, proposal_id)
    if p is None:
        raise ProposalError(404, "not_found", f"提案 {proposal_id} 不存在")
    return p


def _latest_revision(session: Session, p: FmProposal) -> FmProposalRevision:
    rev = session.scalar(
        select(FmProposalRevision)
        .where(FmProposalRevision.proposal_id == p.id)
        .order_by(FmProposalRevision.revision.desc()))
    if rev is None:  # 理论不会发生：创建即有 rev 1
        raise ProposalError(500, "corrupt", "提案没有任何修订")
    return rev


def _next_event_seq(session: Session, p: FmProposal) -> int:
    used = session.scalar(select(FmProposalEvent.sequence)
                          .where(FmProposalEvent.proposal_id == p.id)
                          .order_by(FmProposalEvent.sequence.desc()))
    return (used or 0) + 1


def _add_event(session: Session, p: FmProposal, event_type: str,
               from_status: Optional[str], to_status: Optional[str],
               actor: str = "", note: str = "", detail: Optional[dict] = None) -> None:
    session.add(FmProposalEvent(
        proposal_id=p.id, sequence=_next_event_seq(session, p),
        event_type=event_type, from_status=from_status, to_status=to_status,
        actor=actor or "", note=note or "", detail=detail or {}))


# ---- 创建 / 修订 ------------------------------------------------------------


def create_proposal(session: Session, scenario_id: int, title: str,
                    content: Optional[dict], description: str = "",
                    actor: str = "", note: str = "") -> FmProposal:
    sc, base_snap, base_hash = snapshot_baseline(session, scenario_id)

    target = canonical_snapshot(content) if content is not None else dict(base_snap)
    target_hash = snapshot_hash(target)
    diff = diff_snapshots(base_snap, target)

    p = FmProposal(
        scenario_id=scenario_id, title=title.strip() or "未命名提案",
        description=description or "", status=DRAFT,
        base_version=sc.version, base_snapshot_hash=base_hash,
        created_by=actor or "")
    session.add(p)
    session.flush()  # 取得 p.id
    session.add(FmProposalRevision(
        proposal_id=p.id, revision=1, snapshot=target,
        snapshot_hash=target_hash, diff=diff, note=note, created_by=actor or ""))
    _add_event(session, p, "created", None, DRAFT, actor, note,
               {"base_version": sc.version, "base_snapshot_hash": base_hash,
                "diff_summary": diff["summary"]})
    session.commit()
    session.refresh(p)
    return p


def revise_proposal(session: Session, proposal_id: int, content: dict,
                    actor: str = "", note: str = "") -> FmProposalRevision:
    """草稿内容修订：仅 draft 状态允许；推进修订号并使旧规划产物过期。"""
    p = _get_proposal(session, proposal_id)
    if p.status != DRAFT:
        raise ProposalError(409, "not_editable",
                            f"提案处于 {p.status} 状态，不能直接修改；请先退回草稿",
                            {"status": p.status})
    old_rev = _latest_revision(session, p)
    target = canonical_snapshot(content)
    target_hash = snapshot_hash(target)
    if target_hash == old_rev.snapshot_hash:
        raise ProposalError(409, "no_change", "新修订与当前草稿内容完全相同，未产生新版本")

    # 差异相对上一修订（逐修可回放）；同时保留相对基准的累计差异
    diff_prev = diff_snapshots(old_rev.snapshot, target)
    base_snap = _base_snapshot(session, p)
    diff_base = diff_snapshots(base_snap, target)
    rev = FmProposalRevision(
        proposal_id=p.id, revision=old_rev.revision + 1, snapshot=target,
        snapshot_hash=target_hash,
        diff={"from_revision": old_rev.revision, "vs_previous": diff_prev,
              "vs_base": diff_base},
        note=note, created_by=actor or "")
    session.add(rev)
    _add_event(session, p, "revised", DRAFT, DRAFT, actor, note,
               {"revision": rev.revision,
                "diff_summary": diff_prev["summary"],
                "invalidated_artifacts_of_revision": old_rev.revision})
    session.commit()
    session.refresh(rev)
    return rev


def accept_plan_as_revision(session: Session, proposal_id: int,
                            actor: str = "", note: str = "") -> FmProposalRevision:
    """把当前草稿最新修订上的规划结果采纳为新修订。

    新修订（目标频点）上立即重跑分析与同模式规划，得到绑定“新修订哈希”的
    校验产物：旧修订的规划输入哈希不会被冒充复用。
    """
    p = _get_proposal(session, proposal_id)
    if p.status != DRAFT:
        raise ProposalError(409, "not_editable",
                            f"提案处于 {p.status} 状态，不能修改草稿",
                            {"status": p.status})
    rev = _latest_revision(session, p)
    art = _latest_plan_artifact(session, p, rev.revision)
    if art is None:
        old = session.scalar(
            select(FmProposalArtifact).where(
                FmProposalArtifact.proposal_id == p.id,
                FmProposalArtifact.kind == "plan")
            .order_by(FmProposalArtifact.revision.desc(),
                      FmProposalArtifact.id.desc()))
        if old is not None and old.revision != rev.revision:
            raise ProposalError(409, "plan_expired",
                                f"规划基于修订 {old.revision} 的旧快照，草稿已改动，"
                                "旧规划哈希失效，必须先在当前修订重新规划再采纳",
                                {"plan_revision": old.revision,
                                 "current_revision": rev.revision})
        raise ProposalError(409, "plan_missing",
                            "当前修订还没有规划结果，请先规划",
                            {"revision": rev.revision})
    if art.input_snapshot_hash != rev.snapshot_hash:
        raise ProposalError(409, "plan_expired",
                            "规划输入快照与当前修订不一致，必须重新规划",
                            {"current_revision": rev.revision})
    if not art.payload.get("feasible"):
        raise ProposalError(409, "plan_infeasible",
                            "规划无解，不能采纳为修订")
    mode = art.mode
    target = apply_plan_to_snapshot(rev.snapshot, art.payload["assignments"])
    new_rev = revise_proposal(
        session, proposal_id, _content_from_snapshot(target), actor,
        note or f"采纳 {mode} 规划结果（目标值 "
                f"{art.payload.get('objective_khz')} kHz）")

    # 在新修订上重跑分析/规划，产物绑定新修订快照哈希（非历史哈希复用）
    analysis_result = analyze(_carriers_of(target), _rules_of(target))
    _upsert_artifact(session, p, new_rev.revision, "analysis", None,
                     new_rev.snapshot_hash, analysis_result, False, actor or "")
    plan_result = plan(_carriers_of(target), _rules_of(target),
                       BandLimits(target["band_low_mhz"], target["band_high_mhz"]),
                       mode=mode or "guard_only")
    post_ok = False
    if plan_result["feasible"]:
        planned = [Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                           bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                           polarization=a["polarization"], mask_name=a["mask_name"])
                   for a in plan_result["assignments"]]
        plan_result["post_check"] = analyze(planned, _rules_of(target))
        post_ok = plan_result["post_check"]["counts"]["error"] == 0
    _upsert_artifact(session, p, new_rev.revision, "plan", mode,
                     new_rev.snapshot_hash, plan_result, post_ok, actor or "")
    _add_event(session, p, "plan_accepted", DRAFT, DRAFT, actor,
               f"规划结果固化为修订 {new_rev.revision} 并重新校验",
               {"source_revision": rev.revision,
                "new_revision": new_rev.revision, "mode": mode,
                "input_snapshot_hash": new_rev.snapshot_hash,
                "post_check_ok": post_ok})
    session.commit()
    session.refresh(new_rev)
    return new_rev


def _base_snapshot(session: Session, p: FmProposal) -> dict:
    """取提案锚定的基准快照（优先版本表；兼容 v0 尚未落版本行的情况）。"""
    row = session.scalar(select(ScenarioVersion).where(
        ScenarioVersion.scenario_id == p.scenario_id,
        ScenarioVersion.version == p.base_version))
    if row is not None:
        return row.snapshot
    # 回退：修订 1 的 diff 无法直接还原；只能用当前场景（仅古老数据可能走到）
    sc, snap, h = snapshot_baseline(session, p.scenario_id)
    return snap


# ---- 分析 / 规划产物（绑定快照哈希 + 修订号） -------------------------------


def _upsert_artifact(session: Session, p: FmProposal, revision: int, kind: str,
                     mode: Optional[str], snapshot_h: str, payload: dict,
                     post_check_ok: bool, actor: str) -> FmProposalArtifact:
    existing = session.scalar(select(FmProposalArtifact).where(
        FmProposalArtifact.proposal_id == p.id,
        FmProposalArtifact.revision == revision,
        FmProposalArtifact.kind == kind,
        FmProposalArtifact.mode == mode))
    if existing is None:
        existing = FmProposalArtifact(
            proposal_id=p.id, revision=revision, kind=kind, mode=mode,
            input_snapshot_hash=snapshot_h, payload=payload,
            post_check_ok=post_check_ok, created_by=actor)
        session.add(existing)
    else:
        existing.input_snapshot_hash = snapshot_h
        existing.payload = payload
        existing.post_check_ok = post_check_ok
        existing.created_by = actor
    return existing


def run_analysis(session: Session, proposal_id: int, actor: str = "") -> FmProposalArtifact:
    p = _get_proposal(session, proposal_id)
    rev = _latest_revision(session, p)
    snap = rev.snapshot
    result = analyze(_carriers_of(snap), _rules_of(snap))
    art = _upsert_artifact(session, p, rev.revision, "analysis", None,
                           rev.snapshot_hash, result, False, actor or "")
    _add_event(session, p, "analysis_attached", p.status, p.status, actor, "",
               {"revision": rev.revision, "input_snapshot_hash": rev.snapshot_hash,
                "status": result["status"], "counts": result["counts"]})
    session.commit()
    session.refresh(art)
    return art


def run_plan(session: Session, proposal_id: int, mode: str,
             actor: str = "") -> FmProposalArtifact:
    p = _get_proposal(session, proposal_id)
    rev = _latest_revision(session, p)
    snap = rev.snapshot
    result = plan(_carriers_of(snap), _rules_of(snap),
                  BandLimits(snap["band_low_mhz"], snap["band_high_mhz"]), mode=mode)
    post_ok = False
    if result["feasible"]:
        planned = [Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                           bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                           polarization=a["polarization"], mask_name=a["mask_name"])
                   for a in result["assignments"]]
        result["post_check"] = analyze(planned, _rules_of(snap))
        post_ok = result["post_check"]["counts"]["error"] == 0
    art = _upsert_artifact(session, p, rev.revision, "plan", mode,
                           rev.snapshot_hash, result, post_ok, actor or "")
    _add_event(session, p, "plan_attached", p.status, p.status, actor, "",
               {"revision": rev.revision, "input_snapshot_hash": rev.snapshot_hash,
                "mode": mode, "feasible": result["feasible"],
                "post_check_ok": post_ok})
    session.commit()
    session.refresh(art)
    return art


def _latest_plan_artifact(session: Session, p: FmProposal,
                          revision: int) -> Optional[FmProposalArtifact]:
    return session.scalar(
        select(FmProposalArtifact).where(
            FmProposalArtifact.proposal_id == p.id,
            FmProposalArtifact.kind == "plan",
            FmProposalArtifact.revision == revision)
        .order_by(FmProposalArtifact.id.desc()))


# ---- 状态迁移 ---------------------------------------------------------------


def _transition(session: Session, p: FmProposal, target: str,
                actor: str, note: str, detail: Optional[dict] = None) -> None:
    if p.status not in TRANSITIONS[target]:
        raise ProposalError(409, "invalid_transition",
                            f"不能把 {p.status} 状态的提案迁移到 {target}",
                            {"from": p.status, "to": target})
    old = p.status
    p.status = target
    _add_event(session, p, target, old, target, actor, note, detail)


def review(session: Session, proposal_id: int, actor: str = "",
           note: str = "") -> FmProposal:
    p = _get_proposal(session, proposal_id)
    if p.status == REVIEWED:
        return p  # 幂等
    if p.status in TERMINAL_STATUSES or p.status == APPLIED:
        raise ProposalError(409, "invalid_transition",
                            f"{p.status} 状态的提案不能评审",
                            {"from": p.status, "to": REVIEWED})
    rev = _latest_revision(session, p)
    # 评审要求当前修订至少跑过分析（评审依据）
    analysis = session.scalar(select(FmProposalArtifact).where(
        FmProposalArtifact.proposal_id == p.id,
        FmProposalArtifact.revision == rev.revision,
        FmProposalArtifact.kind == "analysis"))
    if analysis is None or analysis.input_snapshot_hash != rev.snapshot_hash:
        raise ProposalError(409, "analysis_missing",
                            "评审前必须对当前草稿修订运行分析，作为评审依据",
                            {"revision": rev.revision})
    _transition(session, p, REVIEWED, actor, note,
                {"revision": rev.revision,
                 "analysis_snapshot_hash": rev.snapshot_hash})
    session.commit()
    session.refresh(p)
    return p


def reopen(session: Session, proposal_id: int, actor: str = "",
           note: str = "") -> FmProposal:
    p = _get_proposal(session, proposal_id)
    if p.status == DRAFT:
        return p
    _transition(session, p, DRAFT, actor, note or "评审后退回草稿修改")
    session.commit()
    session.refresh(p)
    return p


def cancel(session: Session, proposal_id: int, actor: str = "",
           note: str = "") -> FmProposal:
    p = _get_proposal(session, proposal_id)
    if p.status == CANCELLED:
        return p  # 幂等
    if p.status in (APPLIED, ROLLED_BACK):
        raise ProposalError(409, "invalid_transition",
                            f"已应用的提案请使用回退，不能取消（当前 {p.status}）",
                            {"status": p.status})
    _transition(session, p, CANCELLED, actor, note)
    session.commit()
    session.refresh(p)
    return p


# ---- 应用（乐观并发 + post-check，整体拒绝） --------------------------------


def _record_rejection(session: Session, p: FmProposal, code: str,
                      message: str, detail: dict, actor: str, note: str) -> None:
    """被拒绝的应用尝试也要留审计（独立事务，不触碰场景与提案状态）。"""
    try:
        session.rollback()
        fresh = session.get(FmProposal, p.id)
        if fresh is not None:
            _add_event(session, fresh, "apply_rejected", fresh.status,
                       fresh.status, actor, note,
                       {"reason": code, "message": message, **detail})
            session.commit()
    except Exception:
        session.rollback()


def apply(session: Session, proposal_id: int, actor: str = "",
          note: str = "") -> tuple[FmProposal, bool]:
    """应用提案。返回 (提案, 是否幂等命中)。失败抛 ProposalError，场景不变。"""
    p = _get_proposal(session, proposal_id)
    if p.status == APPLIED:
        return p, True  # 重复应用幂等
    if p.status != REVIEWED:
        raise ProposalError(409, "invalid_transition",
                            f"仅已评审提案可应用（当前 {p.status}）",
                            {"status": p.status})
    if p.status in (CANCELLED, ROLLED_BACK):
        raise ProposalError(409, "invalid_transition",
                            f"{p.status} 状态的提案不能应用", {"status": p.status})

    sc = session.get(Scenario, p.scenario_id)
    if sc is None:
        raise ProposalError(404, "not_found", "提案对应的场景已不存在")

    # 1) 基准乐观并发校验
    if sc.version != p.base_version:
        err = ProposalError(409, "baseline_conflict",
                            f"基准版本已被他人修改（基准锚定 v{p.base_version}，"
                            f"当前 v{sc.version}），应用被整体拒绝",
                            {"base_version": p.base_version,
                             "current_version": sc.version})
        _record_rejection(session, p, err.code, err.message, err.extra, actor, note)
        raise err
    current_hash = snapshot_hash(snapshot_from_scenario(sc))
    if current_hash != p.base_snapshot_hash:
        err = ProposalError(409, "baseline_conflict",
                            "基准内容与提案锚定的快照不一致，应用被整体拒绝",
                            {"base_snapshot_hash": p.base_snapshot_hash,
                             "current_snapshot_hash": current_hash})
        _record_rejection(session, p, err.code, err.message, err.extra, actor, note)
        raise err

    # 2) 规划结果必须属于最新修订且哈希匹配，否则规划已过期
    rev = _latest_revision(session, p)
    art = _latest_plan_artifact(session, p, rev.revision)
    if art is None:
        # 旧修订上若有规划，明确报告“草稿已改动、旧哈希失效”
        old_art = session.scalar(
            select(FmProposalArtifact).where(
                FmProposalArtifact.proposal_id == p.id,
                FmProposalArtifact.kind == "plan")
            .order_by(FmProposalArtifact.revision.desc(),
                      FmProposalArtifact.id.desc()))
        if old_art is not None and old_art.revision != rev.revision:
            raise ProposalError(409, "plan_expired",
                                f"规划基于修订 {old_art.revision} 的旧快照，草稿已改动，"
                                "旧规划哈希失效，必须重新规划",
                                {"plan_revision": old_art.revision,
                                 "current_revision": rev.revision,
                                 "plan_snapshot_hash": old_art.input_snapshot_hash,
                                 "current_snapshot_hash": rev.snapshot_hash})
        raise ProposalError(409, "plan_missing",
                            "当前草稿修订缺少规划结果，必须重新规划才能应用",
                            {"revision": rev.revision})
    if art.input_snapshot_hash != rev.snapshot_hash:
        raise ProposalError(409, "plan_expired",
                            f"规划基于修订 {art.revision} 的旧快照，草稿已改动，"
                            "旧规划哈希失效，必须重新规划",
                            {"plan_revision": art.revision,
                             "current_revision": rev.revision,
                             "plan_snapshot_hash": art.input_snapshot_hash,
                             "current_snapshot_hash": rev.snapshot_hash})
    if not art.payload.get("feasible"):
        raise ProposalError(409, "plan_infeasible", "规划无解，不能应用")

    target = rev.snapshot
    target_hash = rev.snapshot_hash

    # 3) post-check：对提案目标内容重跑完整分析（不信任历史产物里的结论）
    post = analyze(_carriers_of(target), _rules_of(target))
    if post["counts"]["error"] != 0:
        err = ProposalError(409, "post_check_failed",
                            f"post-check 发现 {post['counts']['error']} 项冲突，"
                            "应用被整体拒绝，未写入任何频率",
                            {"counts": post["counts"],
                             "findings": [{"type": f["type"],
                                           "pair": [f["carrier_a"], f["carrier_b"]]}
                                          for f in post["findings"]
                                          if f["severity"] == "error"]})
        _record_rejection(session, p, err.code, err.message, err.extra, actor, note)
        raise err

    # 4) 原子条件更新：版本号仍是锚定值才写入（检查与写入之间的并发兜底）。
    # PostgreSQL/MySQL 下 UPDATE ... WHERE version=:old 单语句原子；
    # 任何条件不满足都整体回滚，不存在部分写入频率。
    old_version = sc.version
    new_version = old_version + 1
    result = session.execute(
        update(Scenario)
        .where(Scenario.id == sc.id, Scenario.version == old_version)
        .values(
            name=target["name"], description=target.get("description", ""),
            band_low_mhz=target["band_low_mhz"],
            band_high_mhz=target["band_high_mhz"],
            guard_required_mhz=target["guard_required_mhz"],
            leakage_limit_dbm=target["leakage_limit_dbm"],
            reuse_policy=dict(target["reuse_policy"]),
            version=new_version,
        ))
    if result.rowcount != 1:
        session.rollback()
        err = ProposalError(409, "baseline_conflict",
                            "基准在校验与写入之间被他人修改，应用被整体拒绝",
                            {"base_version": old_version})
        _record_rejection(session, p, err.code, err.message, err.extra, actor, note)
        raise err

    # 删除旧载波并按目标快照重建（与版本更新同一事务）
    session.query(CarrierRow).filter(CarrierRow.scenario_id == sc.id).delete(
        synchronize_session=False)
    session.add_all([
        CarrierRow(scenario_id=sc.id, position=i, name=c["name"],
                   center_mhz=c["center_mhz"], bandwidth_mhz=c["bandwidth_mhz"],
                   power_dbm=c["power_dbm"], polarization=c["polarization"],
                   mask_name=c["mask_name"])
        for i, c in enumerate(target["carriers"])
    ])
    session.flush()

    session.add(ScenarioVersion(
        scenario_id=sc.id, version=new_version, kind="proposal_applied",
        snapshot=target, snapshot_hash=target_hash, proposal_id=p.id,
        note=note, actor=actor or ""))

    p.status = APPLIED
    p.applied_version = new_version
    _add_event(session, p, "applied", REVIEWED, APPLIED, actor, note, {
        "base_version": old_version, "applied_version": new_version,
        "revision": rev.revision, "input_snapshot_hash": target_hash,
        "plan_mode": art.mode, "post_check": post["counts"]})
    session.commit()
    session.refresh(p)
    return p, False


def rollback(session: Session, proposal_id: int, actor: str = "",
             note: str = "") -> tuple[FmProposal, bool]:
    """回退已应用提案：把场景恢复为提案锚定的基准内容（单事务、乐观校验）。"""
    p = _get_proposal(session, proposal_id)
    if p.status == ROLLED_BACK:
        return p, True  # 幂等
    if p.status != APPLIED:
        raise ProposalError(409, "invalid_transition",
                            f"仅已应用提案可回退（当前 {p.status}）",
                            {"status": p.status})

    sc = session.get(Scenario, p.scenario_id)
    if sc is None:
        raise ProposalError(404, "not_found", "提案对应的场景已不存在")
    # 应用之后若场景又被改动，则不能直接回退（避免覆盖他人的新基准）
    if sc.version != p.applied_version:
        raise ProposalError(409, "baseline_conflict",
                            f"应用版本 v{p.applied_version} 之后场景已推进到 "
                            f"v{sc.version}，回退被拒绝",
                            {"applied_version": p.applied_version,
                             "current_version": sc.version})

    base_version_row = session.scalar(select(ScenarioVersion).where(
        ScenarioVersion.scenario_id == sc.id,
        ScenarioVersion.version == p.base_version))
    if base_version_row is None:
        raise ProposalError(500, "baseline_snapshot_missing",
                            f"找不到基准 v{p.base_version} 的快照，无法重放回退")
    base = base_version_row.snapshot
    base_hash = base_version_row.snapshot_hash

    old_version = sc.version
    new_version = old_version + 1
    result = session.execute(
        update(Scenario)
        .where(Scenario.id == sc.id, Scenario.version == old_version)
        .values(
            name=base["name"], description=base.get("description", ""),
            band_low_mhz=base["band_low_mhz"],
            band_high_mhz=base["band_high_mhz"],
            guard_required_mhz=base["guard_required_mhz"],
            leakage_limit_dbm=base["leakage_limit_dbm"],
            reuse_policy=dict(base["reuse_policy"]),
            version=new_version,
        ))
    if result.rowcount != 1:
        session.rollback()
        raise ProposalError(409, "baseline_conflict",
                            "场景在校验与回退写入之间被修改，回退被整体拒绝",
                            {"expected_version": old_version})
    session.query(CarrierRow).filter(CarrierRow.scenario_id == sc.id).delete(
        synchronize_session=False)
    session.add_all([
        CarrierRow(scenario_id=sc.id, position=i, name=c["name"],
                   center_mhz=c["center_mhz"], bandwidth_mhz=c["bandwidth_mhz"],
                   power_dbm=c["power_dbm"], polarization=c["polarization"],
                   mask_name=c["mask_name"])
        for i, c in enumerate(base["carriers"])
    ])
    session.flush()

    session.add(ScenarioVersion(
        scenario_id=sc.id, version=new_version, kind="rollback",
        snapshot=base, snapshot_hash=base_hash, proposal_id=p.id,
        note=note or f"回退提案 {p.id}，恢复到 v{p.base_version}",
        actor=actor or ""))
    p.status = ROLLED_BACK
    p.rolled_back_to_version = new_version
    _add_event(session, p, "rolled_back", APPLIED, ROLLED_BACK, actor, note, {
        "restored_base_version": p.base_version,
        "from_version": old_version, "new_version": new_version,
        "snapshot_hash": base_hash})
    session.commit()
    session.refresh(p)
    return p, False
