"""版本化调频提案的 FastAPI 路由。

挂在 /api 下：场景版本历史 + 提案 CRUD/工作流/产物/导出。
工作流错误统一映射为带 code 的 JSON：``{"detail": {"code", "message", ...}}``。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import (FmProposal, FmProposalArtifact, FmProposalEvent,
                 FmProposalRevision, Scenario, ScenarioVersion)
from .schemas import (ArtifactOut, EventOut, ProposalCreateIn, ProposalDecisionIn,
                      ProposalOut, ProposalPlanIn, ProposalReviseIn,
                      ProposalSummary, RevisionOut, ScenarioVersionOut)
from .services import proposals as svc
from .services.snapshots import diff_snapshots

router = APIRouter(prefix="/api", tags=["proposals"])


def _error_response(e: svc.ProposalError) -> JSONResponse:
    return JSONResponse(status_code=e.status_code,
                        content={"detail": {"code": e.code, "message": e.message,
                                            **e.extra}})


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _artifact_out(a: FmProposalArtifact) -> ArtifactOut:
    return ArtifactOut(
        id=a.id, revision=a.revision, kind=a.kind, mode=a.mode,
        input_snapshot_hash=a.input_snapshot_hash,
        post_check_ok=a.post_check_ok, payload=a.payload,
        created_at=_iso(a.created_at))


def _event_out(e: FmProposalEvent) -> EventOut:
    return EventOut(sequence=e.sequence, event_type=e.event_type,
                    from_status=e.from_status, to_status=e.to_status,
                    actor=e.actor or "", note=e.note or "",
                    detail=e.detail or {}, created_at=_iso(e.created_at))


def _vs_base_diff(rev: FmProposalRevision) -> dict:
    """修订 1 的 diff 直接相对基准；后续修订的累计差异在 diff.vs_base。"""
    d = rev.diff
    return d if "summary" in d else d.get("vs_base", {})


def _revision_out(s: Session, rev: FmProposalRevision) -> RevisionOut:
    arts = list(s.scalars(select(FmProposalArtifact).where(
        FmProposalArtifact.proposal_id == rev.proposal_id,
        FmProposalArtifact.revision == rev.revision).order_by(FmProposalArtifact.id)))
    return RevisionOut(
        revision=rev.revision, snapshot_hash=rev.snapshot_hash, diff=rev.diff,
        note=rev.note or "", created_by=rev.created_by or "",
        created_at=_iso(rev.created_at),
        artifacts=[_artifact_out(a) for a in arts])


def _proposal_out(s: Session, p: FmProposal) -> ProposalOut:
    revs = list(p.revisions)
    rev = revs[-1]
    base_row = s.scalar(select(ScenarioVersion).where(
        ScenarioVersion.scenario_id == p.scenario_id,
        ScenarioVersion.version == p.base_version))
    base_snap = base_row.snapshot if base_row else rev.snapshot
    sc = s.get(Scenario, p.scenario_id)
    current_version = sc.version if sc else p.base_version
    return ProposalOut(
        id=p.id, scenario_id=p.scenario_id, title=p.title,
        description=p.description or "", status=p.status,
        base_version=p.base_version, base_snapshot_hash=p.base_snapshot_hash,
        applied_version=p.applied_version,
        rolled_back_to_version=p.rolled_back_to_version,
        created_by=p.created_by or "", created_at=_iso(p.created_at),
        updated_at=_iso(p.updated_at),
        base_snapshot=base_snap, current_revision=rev.revision,
        current_snapshot=rev.snapshot, current_snapshot_hash=rev.snapshot_hash,
        diff_base=_vs_base_diff(rev),
        revisions=[_revision_out(s, r) for r in revs],
        events=[_event_out(e) for e in p.events],
        scenario_current_version=current_version,
        baseline_changed=current_version != p.base_version)


def _summary_out(s: Session, p: FmProposal) -> ProposalSummary:
    revs = list(p.revisions)
    rev = revs[-1]
    diff = _vs_base_diff(rev)
    return ProposalSummary(
        id=p.id, scenario_id=p.scenario_id, title=p.title, status=p.status,
        base_version=p.base_version, revision_count=len(revs),
        applied_version=p.applied_version,
        rolled_back_to_version=p.rolled_back_to_version,
        created_by=p.created_by or "", created_at=_iso(p.created_at),
        updated_at=_iso(p.updated_at), diff_summary=diff.get("summary"))


# ---- 场景版本历史 -----------------------------------------------------------


@router.get("/scenarios/{scenario_id}/versions",
            response_model=list[ScenarioVersionOut])
def list_scenario_versions(scenario_id: int):
    with Session(svc_engine) as s:
        if s.get(Scenario, scenario_id) is None:
            return JSONResponse(status_code=404,
                                content={"detail": "场景不存在"})
        rows = list(s.scalars(select(ScenarioVersion).where(
            ScenarioVersion.scenario_id == scenario_id)
            .order_by(ScenarioVersion.version)))
        out = []
        prev_snap = None
        for r in rows:
            d = diff_snapshots(prev_snap, r.snapshot) if prev_snap else None
            out.append(ScenarioVersionOut(
                version=r.version, kind=r.kind, snapshot_hash=r.snapshot_hash,
                proposal_id=r.proposal_id, note=r.note or "", actor=r.actor or "",
                created_at=_iso(r.created_at), snapshot=None,
                diff_from_previous=d))
            prev_snap = r.snapshot
        return out


@router.get("/scenarios/{scenario_id}/versions/{version}",
            response_model=ScenarioVersionOut)
def get_scenario_version(scenario_id: int, version: int):
    with Session(svc_engine) as s:
        r = s.scalar(select(ScenarioVersion).where(
            ScenarioVersion.scenario_id == scenario_id,
            ScenarioVersion.version == version))
        if r is None:
            return JSONResponse(status_code=404,
                                content={"detail": f"版本 v{version} 不存在"})
        prev = s.scalar(select(ScenarioVersion).where(
            ScenarioVersion.scenario_id == scenario_id,
            ScenarioVersion.version == version - 1))
        d = diff_snapshots(prev.snapshot, r.snapshot) if prev else None
        return ScenarioVersionOut(
            version=r.version, kind=r.kind, snapshot_hash=r.snapshot_hash,
            proposal_id=r.proposal_id, note=r.note or "", actor=r.actor or "",
            created_at=_iso(r.created_at), snapshot=r.snapshot,
            diff_from_previous=d)


# ---- 提案列表/详情/创建/修订 ------------------------------------------------


@router.get("/proposals", response_model=list[ProposalSummary])
def list_proposals(scenario_id: int | None = None):
    with Session(svc_engine) as s:
        stmt = select(FmProposal).order_by(FmProposal.id)
        if scenario_id is not None:
            stmt = stmt.where(FmProposal.scenario_id == scenario_id)
        return [_summary_out(s, p) for p in s.scalars(stmt)]


@router.post("/proposals", response_model=ProposalOut)
def create_proposal(req: ProposalCreateIn):
    with Session(svc_engine) as s:
        content = req.content.model_dump() if req.content else None
        try:
            p = svc.create_proposal(s, req.scenario_id, req.title, content,
                                    description=req.description, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, p)


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: int):
    with Session(svc_engine) as s:
        p = s.get(FmProposal, proposal_id)
        if p is None:
            return JSONResponse(status_code=404, content={"detail": "提案不存在"})
        return _proposal_out(s, p)


@router.put("/proposals/{proposal_id}", response_model=ProposalOut)
def revise_proposal(proposal_id: int, req: ProposalReviseIn):
    with Session(svc_engine) as s:
        try:
            svc.revise_proposal(s, proposal_id, req.content.model_dump(),
                                note=req.note)
        except svc.ProposalError as e:
            return _error_response(e)
        p = s.get(FmProposal, proposal_id)
        return _proposal_out(s, p)


# ---- 分析 / 规划产物 --------------------------------------------------------


@router.post("/proposals/{proposal_id}/analysis", response_model=ArtifactOut)
def proposal_analysis(proposal_id: int):
    with Session(svc_engine) as s:
        try:
            art = svc.run_analysis(s, proposal_id)
        except svc.ProposalError as e:
            return _error_response(e)
        return _artifact_out(art)


@router.post("/proposals/{proposal_id}/plan", response_model=ArtifactOut)
def proposal_plan(proposal_id: int, req: ProposalPlanIn):
    with Session(svc_engine) as s:
        try:
            art = svc.run_plan(s, proposal_id, req.mode)
        except svc.ProposalError as e:
            return _error_response(e)
        return _artifact_out(art)


@router.post("/proposals/{proposal_id}/accept-plan", response_model=ProposalOut)
def proposal_accept_plan(proposal_id: int, req: ProposalDecisionIn):
    with Session(svc_engine) as s:
        try:
            svc.accept_plan_as_revision(s, proposal_id, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, s.get(FmProposal, proposal_id))


# ---- 状态迁移 ---------------------------------------------------------------


@router.post("/proposals/{proposal_id}/review", response_model=ProposalOut)
def review_proposal(proposal_id: int, req: ProposalDecisionIn):
    with Session(svc_engine) as s:
        try:
            p = svc.review(s, proposal_id, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, p)


@router.post("/proposals/{proposal_id}/reopen", response_model=ProposalOut)
def reopen_proposal(proposal_id: int, req: ProposalDecisionIn):
    with Session(svc_engine) as s:
        try:
            p = svc.reopen(s, proposal_id, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, p)


@router.post("/proposals/{proposal_id}/cancel", response_model=ProposalOut)
def cancel_proposal(proposal_id: int, req: ProposalDecisionIn):
    with Session(svc_engine) as s:
        try:
            p = svc.cancel(s, proposal_id, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, p)


@router.post("/proposals/{proposal_id}/apply", response_model=ProposalOut)
def apply_proposal(proposal_id: int, req: ProposalDecisionIn):
    with Session(svc_engine) as s:
        try:
            p, _ = svc.apply(s, proposal_id, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, p)


@router.post("/proposals/{proposal_id}/rollback", response_model=ProposalOut)
def rollback_proposal(proposal_id: int, req: ProposalDecisionIn):
    with Session(svc_engine) as s:
        try:
            p, _ = svc.rollback(s, proposal_id, note=req.note)
        except svc.ProposalError as e:
            s.rollback()
            return _error_response(e)
        return _proposal_out(s, p)


# ---- 导出：基准 + 已应用版本 + 每次决定的依据 -------------------------------


@router.get("/proposals/{proposal_id}/export")
def export_proposal(proposal_id: int):
    with Session(svc_engine) as s:
        p = s.get(FmProposal, proposal_id)
        if p is None:
            return JSONResponse(status_code=404, content={"detail": "提案不存在"})
        out = _proposal_out(s, p).model_dump()

        versions = list(s.scalars(select(ScenarioVersion).where(
            ScenarioVersion.scenario_id == p.scenario_id)
            .order_by(ScenarioVersion.version)))
        prev = None
        version_log = []
        for r in versions:
            d = diff_snapshots(prev, r.snapshot) if prev else None
            version_log.append({
                "version": r.version, "kind": r.kind,
                "snapshot_hash": r.snapshot_hash, "proposal_id": r.proposal_id,
                "note": r.note, "actor": r.actor,
                "created_at": _iso(r.created_at), "snapshot": r.snapshot,
                "diff_from_previous": d,
            })
            prev = r.snapshot

        artifacts = list(s.scalars(select(FmProposalArtifact).where(
            FmProposalArtifact.proposal_id == proposal_id)
            .order_by(FmProposalArtifact.id)))

        return {
            "export_kind": "fm_proposal_bundle",
            "proposal": out,
            # 原基准快照（创建时锚定）
            "baseline": {
                "version": p.base_version,
                "snapshot_hash": p.base_snapshot_hash,
                "snapshot": out["base_snapshot"],
            },
            # 已应用版本（若有）
            "applied": {
                "version": p.applied_version,
                "snapshot": (next((v["snapshot"] for v in version_log
                                   if v["version"] == p.applied_version), None)
                             if p.applied_version is not None else None),
            },
            "rolled_back_to_version": p.rolled_back_to_version,
            "scenario_versions": version_log,
            "artifacts": [_artifact_out(a).model_dump() for a in artifacts],
            "decisions": [_event_out(e).model_dump() for e in p.events],
        }


def configure(engine) -> None:
    """由 main 调用，注入数据库 engine（避免模块级循环依赖）。"""
    global svc_engine
    svc_engine = engine


svc_engine = None
