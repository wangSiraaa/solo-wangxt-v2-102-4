"""调频提案 / 基准版本的 FastAPI 路由。

与主应用共用同一个 engine（main.app），测试用 monkeypatch 替换 main.engine
后，本模块通过 getattr(main, "engine") 动态取，因此同样被覆盖。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import proposals_service as svc
from .db import (Proposal, ProposalArtifact, ProposalEvent, ProposalSnapshot,
                 Scenario, ScenarioVersion)
from .schemas import (ActorIn, ArtifactOut, DecisionIn, EventOut,
                      ProposalCreate, ProposalDraftIn, ProposalOut,
                      ProposalPlanIn, ProposalReviewIn, ProposalSummary,
                      ScenarioVersionDetail, ScenarioVersionOut, SnapshotOut)

router = APIRouter(prefix="/api", tags=["proposals"])


def _session() -> Session:
    # 延迟导入：main 在模块加载阶段才创建 engine，且测试会 monkeypatch 它
    from . import main
    return Session(main.engine)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _artifact_out(a: ProposalArtifact) -> ArtifactOut:
    return ArtifactOut(
        id=a.id, kind=a.kind, revision=a.revision, input_hash=a.input_hash,
        mode=a.mode, actor=a.actor, created_at=_iso(a.created_at),
        result=a.result)


def _snapshot_out(sn: ProposalSnapshot) -> SnapshotOut:
    return SnapshotOut(
        id=sn.id, revision=sn.revision, content_hash=sn.content_hash,
        content=sn.content, diff_vs_base=sn.diff_vs_base, note=sn.note,
        created_at=_iso(sn.created_at))


def _event_out(e: ProposalEvent) -> EventOut:
    return EventOut(id=e.id, seq=e.seq, type=e.type, actor=e.actor,
                    note=e.note, detail=e.detail, created_at=_iso(e.created_at))


def proposal_to_out(s: Session, p: Proposal) -> ProposalOut:
    snap = svc.current_snapshot(s, p)
    ana = svc.current_analysis(s, p)
    plan = svc.current_plan(s, p)
    events = list(s.scalars(select(ProposalEvent)
                            .where(ProposalEvent.proposal_id == p.id)
                            .order_by(ProposalEvent.seq)))
    return ProposalOut(
        id=p.id, scenario_id=p.scenario_id, title=p.title,
        rationale=p.rationale, status=p.status,
        base_revision=p.base_revision, base_hash=p.base_hash,
        applied_revision=p.applied_revision, created_by=p.created_by,
        created_at=_iso(p.created_at), updated_at=_iso(p.updated_at),
        applied_at=_iso(p.applied_at),
        current_revision=snap.revision, current_hash=snap.content_hash,
        diff_vs_base=snap.diff_vs_base,
        latest_snapshot=_snapshot_out(snap),
        analysis=_artifact_out(ana) if ana else None,
        plan=_artifact_out(plan) if plan else None,
        events=[_event_out(e) for e in events])


def _call(fn, *args):
    """统一把服务层 ProposalError 映射成 HTTPException。"""
    try:
        with _session() as s:
            return fn(s, *args)
    except svc.ProposalError as e:
        raise HTTPException(status_code=e.status_code,
                            detail={"code": e.code, "message": e.message})


# ---- 提案 CRUD 与动作 -------------------------------------------------------

@router.get("/proposals", response_model=list[ProposalSummary])
def list_proposals(scenario_id: int | None = Query(default=None)):
    with _session() as s:
        stmt = select(Proposal).order_by(Proposal.id.desc())
        if scenario_id is not None:
            stmt = stmt.where(Proposal.scenario_id == scenario_id)
        out = []
        for p in s.scalars(stmt):
            snap = svc.current_snapshot(s, p)
            plan = svc.current_plan(s, p)
            ana = svc.current_analysis(s, p)
            out.append(ProposalSummary(
                id=p.id, scenario_id=p.scenario_id, title=p.title,
                status=p.status, base_revision=p.base_revision,
                base_hash=p.base_hash, current_revision=snap.revision,
                current_hash=snap.content_hash,
                applied_revision=p.applied_revision,
                diff_summary=svc.moves_summary(snap.diff_vs_base),
                plan_fresh=plan is not None and bool(plan.result.get("feasible")),
                has_analysis=ana is not None,
                created_by=p.created_by, created_at=_iso(p.created_at)))
        return out


@router.post("/proposals", response_model=ProposalOut)
def create_proposal(req: ProposalCreate):
    content = svc.payload_content_dict(req.content) if req.content else None
    p = _call(svc.create_proposal, req.scenario_id, req.title,
              req.rationale, content, req.note, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: int):
    def action(s, pid):
        return proposal_to_out(s, svc.get_proposal(s, pid))
    return _call(action, proposal_id)


@router.put("/proposals/{proposal_id}/draft", response_model=ProposalOut)
def update_draft(proposal_id: int, req: ProposalDraftIn):
    p = _call(svc.update_draft, proposal_id,
              svc.payload_content_dict(req.content), req.note, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.post("/proposals/{proposal_id}/analyze", response_model=ProposalOut)
def run_analysis(proposal_id: int, req: ActorIn):
    p = _call(svc.run_analysis, proposal_id, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.post("/proposals/{proposal_id}/plan", response_model=ProposalOut)
def run_plan(proposal_id: int, req: ProposalPlanIn):
    p = _call(svc.run_plan, proposal_id, req.mode, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.post("/proposals/{proposal_id}/review", response_model=ProposalOut)
def review_proposal(proposal_id: int, req: ProposalReviewIn):
    p = _call(svc.review_proposal, proposal_id, req.note, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.post("/proposals/{proposal_id}/apply", response_model=ProposalOut)
def apply_proposal(proposal_id: int, req: DecisionIn):
    p = _call(svc.apply_proposal, proposal_id, req.note, req.actor,
              req.expected_base_revision)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.post("/proposals/{proposal_id}/cancel", response_model=ProposalOut)
def cancel_proposal(proposal_id: int, req: DecisionIn):
    p = _call(svc.cancel_proposal, proposal_id, req.note, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.post("/proposals/{proposal_id}/rollback", response_model=ProposalOut)
def rollback_proposal(proposal_id: int, req: DecisionIn):
    p = _call(svc.rollback_proposal, proposal_id, req.note, req.actor)
    with _session() as s:
        return proposal_to_out(s, s.get(Proposal, p.id))


@router.get("/proposals/{proposal_id}/export")
def export_proposal(proposal_id: int):
    def action(s, pid):
        return svc.export_proposal(s, pid)
    return _call(action, proposal_id)


# ---- 基准版本链 -------------------------------------------------------------

@router.get("/scenarios/{scenario_id}/versions",
            response_model=list[ScenarioVersionOut])
def list_versions(scenario_id: int):
    with _session() as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        rows = list(s.scalars(
            select(ScenarioVersion)
            .where(ScenarioVersion.scenario_id == scenario_id)
            .order_by(ScenarioVersion.revision)))
        return [ScenarioVersionOut(
            id=v.id, revision=v.revision, kind=v.kind, label=v.label,
            content_hash=v.content_hash, proposal_id=v.proposal_id,
            actor=v.actor, created_at=_iso(v.created_at)) for v in rows]


@router.get("/scenarios/{scenario_id}/versions/{revision}",
            response_model=ScenarioVersionDetail)
def get_version(scenario_id: int, revision: int):
    with _session() as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        v = s.scalar(select(ScenarioVersion).where(
            ScenarioVersion.scenario_id == scenario_id,
            ScenarioVersion.revision == revision))
        if v is None:
            raise HTTPException(404, "该版本不存在")
        return ScenarioVersionDetail(
            id=v.id, revision=v.revision, kind=v.kind, label=v.label,
            content_hash=v.content_hash, proposal_id=v.proposal_id,
            actor=v.actor, created_at=_iso(v.created_at), content=v.content)
