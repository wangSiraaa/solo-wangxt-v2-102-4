"""版本化调频提案工作流测试（内存 SQLite，无需 PostgreSQL）。

覆盖验收：
1) 多频率移动提案：创建 → 规划 → 采纳 → 分析 → 评审 → 应用 → 新场景完整分析通过；
2) 创建提案后直接修改基准：应用被冲突拦截，基准不变（且整体无部分写入）；
3) 草稿改动后旧规划哈希失效：必须重新规划/重新采纳才能应用；
4) 重复应用/取消/回退幂等；导出同时含原基准、已应用版本与每次决定依据。
另含状态机非法迁移、取消已应用提案、无差异修订等负路径。
"""
import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.db import (Base, CarrierRow, FmProposalArtifact, MaskRow,
                    Scenario, ScenarioVersion)
from app.seed import DEMO_CARRIERS, DEMO_POLICY
from app.services.masks import MASKS


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for m in MASKS.values():
            s.add(MaskRow(name=m.name, points=[list(p) for p in m.points],
                          span_mhz=m.span_mhz, description=m.description))
        sc = Scenario(name="教学演示场景", description="t", band_low_mhz=80,
                      band_high_mhz=300, guard_required_mhz=1.0,
                      leakage_limit_dbm=-45.0, reuse_policy=DEMO_POLICY,
                      version=0,
                      carriers=[CarrierRow(**kw) for kw in DEMO_CARRIERS])
        s.add(sc)
        s.flush()
        snap = main.snapshot_from_scenario(sc)
        s.add(ScenarioVersion(scenario_id=sc.id, version=0, kind="baseline",
                              snapshot=snap,
                              snapshot_hash=main.snapshot_hash(snap),
                              note="v0"))
        s.commit()
    monkeypatch.setattr(main, "engine", engine)
    main.configure_proposals(engine)
    with TestClient(main.app) as c:
        yield c


def _baseline(client, sid=1):
    sc = client.get(f"/api/scenarios/{sid}").json()
    return {
        "name": sc["name"], "description": sc["description"],
        "band_low_mhz": sc["band_low_mhz"], "band_high_mhz": sc["band_high_mhz"],
        "guard_required_mhz": sc["guard_required_mhz"],
        "leakage_limit_dbm": sc["leakage_limit_dbm"],
        "reuse_policy": sc["reuse_policy"],
        "carriers": [{k: c[k] for k in
                      ("name", "center_mhz", "bandwidth_mhz", "power_dbm",
                       "polarization", "mask_name")} for c in sc["carriers"]],
    }


def _move(content, mapping):
    """按载波名移动中心频率。"""
    content = copy.deepcopy(content)
    for c in content["carriers"]:
        if c["name"] in mapping:
            c["center_mhz"] = mapping[c["name"]]
    return content


def _err(r):
    d = r.json()["detail"]
    return d if isinstance(d, dict) else {"message": d}


def _fully_apply(client, pid):
    assert client.post(f"/api/proposals/{pid}/analysis").status_code == 200
    r = client.post(f"/api/proposals/{pid}/review", json={"note": "评审通过"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/proposals/{pid}/apply", json={"note": "教师确认应用"})
    assert r.status_code == 200, r.text
    return r.json()


# ---- 验收 1：多频率移动 → 应用 → 新场景完整分析 -----------------------------


def test_full_flow_multi_move_apply_and_analyze(client):
    base = _baseline(client)
    # 三条频率移动：拆开 C1/C2、C4/C5、C7/C8（解决保护带/尾部越界）
    draft = _move(base, {"C2": 110.0, "C5": 140.0, "C8": 162.0})
    r = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "拆分冲突载波对",
        "description": "三处频率移动", "content": draft})
    assert r.status_code == 200, r.text
    p = r.json()
    pid = p["id"]
    assert p["status"] == "draft"
    assert p["base_version"] == 0
    assert p["diff_base"]["summary"]["carriers_moved"] == 3
    moved = {c["carrier"] for c in p["diff_base"]["carrier_changes"]
             if "center_mhz" in c["changes"]}
    assert moved == {"C2", "C5", "C8"}

    # 规划（mask_aware）必须可行且 post-check 无冲突
    plan = client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    assert plan.status_code == 200
    assert plan.json()["payload"]["feasible"]
    assert plan.json()["post_check_ok"]
    assert plan.json()["input_snapshot_hash"] == p["current_snapshot_hash"]
    assert plan.json()["revision"] == 1

    # 采纳规划 → 新修订，且新修订产物绑定新哈希
    r = client.post(f"/api/proposals/{pid}/accept-plan", json={})
    assert r.status_code == 200, r.text
    p2 = r.json()
    assert p2["current_revision"] == 2
    rev2 = next(x for x in p2["revisions"] if x["revision"] == 2)
    plan_art = next(a for a in rev2["artifacts"] if a["kind"] == "plan")
    assert plan_art["input_snapshot_hash"] == p2["current_snapshot_hash"]
    assert plan_art["post_check_ok"]

    # 应用
    applied = _fully_apply(client, pid)
    assert applied["status"] == "applied"
    assert applied["applied_version"] == 1

    # 新场景通过完整分析（走现有无状态 /api/analyze 口径）
    sc = client.get("/api/scenarios/1").json()
    assert sc["version"] == 1
    body = {"carriers": sc["carriers"],
            "rules": {"guard_required_mhz": sc["guard_required_mhz"],
                      "leakage_limit_dbm": sc["leakage_limit_dbm"],
                      "reuse_policy": sc["reuse_policy"]},
            "plot_grid_mhz": 0.05}
    res = client.post("/api/analyze", json=body).json()
    assert res["counts"]["error"] == 0
    # 应用后的场景与提案最终修订快照逐载波一致
    final_by_name = {c["name"]: c for c in p2["current_snapshot"]["carriers"]}
    for b in res["bands"]:
        assert b["center_mhz"] == final_by_name[b["name"]]["center_mhz"]
    # 三对冲突载波相对基准都被移开（位置发生变化）
    base_by_name = {c["name"]: c for c in base["carriers"]}
    for n in ("C2", "C5", "C8"):
        assert final_by_name[n]["center_mhz"] != base_by_name[n]["center_mhz"]

    # 版本历史里有 v0 baseline 与 v1 proposal_applied，且差异可定位移动
    versions = client.get("/api/scenarios/1/versions").json()
    assert [v["version"] for v in versions] == [0, 1]
    assert versions[1]["kind"] == "proposal_applied"
    d = versions[1]["diff_from_previous"]
    # v1 内容 = 采纳规划后的最终修订；移动数应与最终修订相对基准一致
    final_moved = {c["carrier"] for c in p2["diff_base"]["carrier_changes"]
                   if "center_mhz" in c["changes"]}
    version_moved = {c["carrier"] for c in d["carrier_changes"]
                     if "center_mhz" in c["changes"]}
    assert version_moved == final_moved
    assert {"C2", "C5", "C8"} <= version_moved

    # 审计事件链完整
    types = [e["event_type"] for e in applied["events"]]
    assert types[0] == "created"
    assert "applied" in types and types[-1] == "applied"
    applied_ev = applied["events"][-1]
    assert applied_ev["detail"]["post_check"]["error"] == 0


# ---- 验收 2：基准被他人直接修改 → 应用冲突拦截，基准不变 ----------------------


def test_apply_conflicts_when_baseline_changed(client):
    base = _baseline(client)
    draft = _move(base, {"C2": 110.0, "C5": 140.0, "C8": 162.0})
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p", "content": draft}).json()["id"]
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    client.post(f"/api/proposals/{pid}/accept-plan", json={})
    client.post(f"/api/proposals/{pid}/analysis")
    client.post(f"/api/proposals/{pid}/review", json={})

    # 他人直接改基准（推进 v0 → v1）
    other = _move(base, {"C3": 118.0})
    r = client.put("/api/scenarios/1", json=other)
    assert r.status_code == 200
    assert r.json()["version"] == 1
    baseline_after_edit = _baseline(client)

    # 应用被拦截
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert r.status_code == 409
    assert _err(r)["code"] == "baseline_conflict"

    # 基准完全不变（含载波频率与版本号），无部分写入
    assert _baseline(client) == baseline_after_edit
    assert client.get("/api/scenarios/1").json()["version"] == 1

    # 被拒绝的尝试也有审计
    p = client.get(f"/api/proposals/{pid}").json()
    assert p["status"] == "reviewed"
    rej = [e for e in p["events"] if e["event_type"] == "apply_rejected"]
    assert len(rej) == 1 and rej[0]["detail"]["reason"] == "baseline_conflict"

    # 同内容基准（版本号不同）也必须拦截：构造版本号相同但哈希不同
    # （由提案路径产生的 v1 不影响此处——直接用回退场景验证哈希校验）


def test_apply_conflicts_when_baseline_hash_differs_even_same_version(client):
    base = _baseline(client)
    draft = _move(base, {"C2": 110.0})
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p", "content": draft}).json()["id"]
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    client.post(f"/api/proposals/{pid}/accept-plan", json={})
    client.post(f"/api/proposals/{pid}/analysis")
    client.post(f"/api/proposals/{pid}/review", json={})

    # 手工把场景版本号改回 0 且内容换掉（版本号相同但哈希不一致）
    with Session(main.engine) as s:
        sc = s.get(Scenario, 1)
        sc.version = 0
        c3 = next(c for c in sc.carriers if c.name == "C3")
        c3.center_mhz = 119.25
        s.commit()
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert r.status_code == 409
    assert _err(r)["code"] == "baseline_conflict"


# ---- 验收 3：草稿改动后旧规划哈希失效，必须重新规划 --------------------------


def test_stale_plan_hash_blocks_apply_after_revision(client):
    base = _baseline(client)
    draft = _move(base, {"C2": 110.0, "C5": 140.0})
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p", "content": draft}).json()["id"]
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})

    # 评审后退回草稿并改动 → rev 2，旧规划属于 rev 1
    client.post(f"/api/proposals/{pid}/analysis")
    client.post(f"/api/proposals/{pid}/review", json={})
    client.post(f"/api/proposals/{pid}/reopen", json={})
    draft2 = _move(draft, {"C8": 162.0})
    r = client.put(f"/api/proposals/{pid}", json={"content": draft2})
    assert r.status_code == 200
    assert r.json()["current_revision"] == 2

    # rev 1 上的规划输入哈希 != rev 2 哈希：直接评审（缺 rev2 分析）会被拦
    client.post(f"/api/proposals/{pid}/analysis")
    client.post(f"/api/proposals/{pid}/review", json={})
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert r.status_code == 409
    # rev1 有规划、rev2 没有：旧规划哈希已失效，必须重新规划
    assert _err(r)["code"] == "plan_expired"
    assert _err(r)["plan_revision"] == 1 and _err(r)["current_revision"] == 2

    # 必须重新规划 → 重新采纳（得到 rev3 且产物哈希匹配）才能应用
    client.post(f"/api/proposals/{pid}/reopen", json={})
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    r = client.post(f"/api/proposals/{pid}/accept-plan", json={})
    assert r.status_code == 200
    p = r.json()
    assert p["current_revision"] == 3
    client.post(f"/api/proposals/{pid}/analysis")
    client.post(f"/api/proposals/{pid}/review", json={})
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"


def test_noop_revision_rejected(client):
    base = _baseline(client)
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p", "content": _move(base, {"C2": 110.0})}
        ).json()["id"]
    r = client.put(f"/api/proposals/{pid}",
                   json={"content": _move(base, {"C2": 110.0})})
    assert r.status_code == 409
    assert _err(r)["code"] == "no_change"


def test_accept_plan_rejects_stale_plan_after_revision(client):
    """修订推进后，未在新修订上重新规划就“采纳”必须被 plan_expired 拦截，
    不能把旧修订的频点固化进新快照（否则应用时 post-check 才暴露问题）。"""
    base = _baseline(client)
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p", "content": _move(base, {"C2": 110.0})}
        ).json()["id"]
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    # 改动草稿 → rev2；rev1 的规划不能被采纳
    r = client.put(f"/api/proposals/{pid}",
                   json={"content": _move(base, {"C2": 110.0, "C5": 140.0})})
    assert r.status_code == 200
    r = client.post(f"/api/proposals/{pid}/accept-plan", json={})
    assert r.status_code == 409
    assert _err(r)["code"] == "plan_expired"
    assert client.get(f"/api/proposals/{pid}").json()["current_revision"] == 2

    # 全新提案从未规划就采纳 → plan_missing
    pid2 = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p2", "content": _move(base, {"C2": 110.0})}
        ).json()["id"]
    r = client.post(f"/api/proposals/{pid2}/accept-plan", json={})
    assert r.status_code == 409
    assert _err(r)["code"] == "plan_missing"


# ---- 验收 4：幂等性、回退与导出 ---------------------------------------------


def test_apply_cancel_rollback_idempotent_and_export(client):
    base = _baseline(client)
    draft = _move(base, {"C2": 110.0, "C5": 140.0, "C8": 162.0})
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p", "content": draft}).json()["id"]
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    client.post(f"/api/proposals/{pid}/accept-plan", json={})
    _fully_apply(client, pid)

    # 重复应用幂等：状态/应用版本不变，不产生新场景版本
    r = client.post(f"/api/proposals/{pid}/apply", json={"note": "重复"})
    assert r.status_code == 200
    assert r.json()["status"] == "applied"
    assert client.get("/api/scenarios/1").json()["version"] == 1
    versions = client.get("/api/scenarios/1/versions").json()
    assert [v["version"] for v in versions] == [0, 1]

    # 回退：场景恢复到锚定基准 v0 内容，产生 v2 rollback 快照
    r = client.post(f"/api/proposals/{pid}/rollback",
                    json={"note": "教师决定回退"})
    assert r.status_code == 200
    assert r.json()["status"] == "rolled_back"
    sc = client.get("/api/scenarios/1").json()
    assert sc["version"] == 2
    assert {b["center_mhz"] for b in
            [next(c for c in sc["carriers"] if c["name"] == n)
             for n in ("C2", "C5", "C8")]} == {104.5, 134.5, 156.0}

    # 重复回退幂等
    r = client.post(f"/api/proposals/{pid}/rollback", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "rolled_back"
    assert client.get("/api/scenarios/1").json()["version"] == 2

    # 导出同时包含原基准、已应用版本、全部版本与每次决定依据
    final_p = client.get(f"/api/proposals/{pid}").json()
    bundle = client.get(f"/api/proposals/{pid}/export").json()
    assert bundle["baseline"]["version"] == 0
    assert bundle["applied"]["version"] == 1
    # 已应用版本 = 提案最终修订（采纳规划后）的快照内容
    applied_snapshot = bundle["applied"]["snapshot"]
    final_snapshot = final_p["current_snapshot"]
    assert applied_snapshot["carriers"] == final_snapshot["carriers"]
    # 原基准保留：C2 仍在 104.5，且与已应用版本的频点不同
    base_c2 = next(c for c in bundle["baseline"]["snapshot"]["carriers"]
                   if c["name"] == "C2")
    applied_c2 = next(c for c in applied_snapshot["carriers"] if c["name"] == "C2")
    assert base_c2["center_mhz"] == 104.5
    assert applied_c2["center_mhz"] != base_c2["center_mhz"]
    assert {v["version"] for v in bundle["scenario_versions"]} == {0, 1, 2}
    decision_types = {d["event_type"] for d in bundle["decisions"]}
    assert {"created", "reviewed", "applied", "rolled_back"} <= decision_types
    # 应用决定带 post-check 依据
    applied_dec = next(d for d in bundle["decisions"] if d["event_type"] == "applied")
    assert applied_dec["detail"]["post_check"]["error"] == 0
    # 规划产物绑定哈希与修订号
    assert all(a["input_snapshot_hash"] and a["revision"] >= 1
               for a in bundle["artifacts"])


def test_cancel_idempotent_and_blocks_apply(client):
    base = _baseline(client)
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p",
        "content": _move(base, {"C2": 110.0})}).json()["id"]
    r1 = client.post(f"/api/proposals/{pid}/cancel", json={"note": "不要了"})
    assert r1.status_code == 200 and r1.json()["status"] == "cancelled"
    r2 = client.post(f"/api/proposals/{pid}/cancel", json={})
    assert r2.status_code == 200 and r2.json()["status"] == "cancelled"
    # 取消后不能应用
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert r.status_code == 409 and _err(r)["code"] == "invalid_transition"
    # 取消是幂等的：只产生一次 cancelled 事件
    p = client.get(f"/api/proposals/{pid}").json()
    assert [e["event_type"] for e in p["events"]].count("cancelled") == 1


def test_cannot_cancel_applied_proposal(client):
    base = _baseline(client)
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p",
        "content": _move(base, {"C2": 110.0, "C5": 140.0, "C8": 162.0})}
        ).json()["id"]
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    client.post(f"/api/proposals/{pid}/accept-plan", json={})
    _fully_apply(client, pid)
    r = client.post(f"/api/proposals/{pid}/cancel", json={})
    assert r.status_code == 409


def test_review_requires_analysis(client):
    base = _baseline(client)
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p",
        "content": _move(base, {"C2": 110.0})}).json()["id"]
    r = client.post(f"/api/proposals/{pid}/review", json={})
    assert r.status_code == 409
    assert _err(r)["code"] == "analysis_missing"


def test_review_idempotent(client):
    base = _baseline(client)
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "p",
        "content": _move(base, {"C2": 110.0})}).json()["id"]
    client.post(f"/api/proposals/{pid}/analysis")
    r1 = client.post(f"/api/proposals/{pid}/review", json={})
    r2 = client.post(f"/api/proposals/{pid}/review", json={})
    assert r1.json()["status"] == r2.json()["status"] == "reviewed"
    p = client.get(f"/api/proposals/{pid}").json()
    assert [e["event_type"] for e in p["events"]].count("reviewed") == 1


def test_post_check_failure_rejects_entire_apply(client):
    """直接构造一个仍有同极化重叠的草稿：应用时 post-check 必须整体拒绝。"""
    base = _baseline(client)
    draft = _move(base, {"C10": 170.0})  # C9/C10 完全重叠
    pid = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "坏提案", "content": draft}).json()["id"]
    client.post(f"/api/proposals/{pid}/analysis")
    client.post(f"/api/proposals/{pid}/review", json={})
    # 没有规划产物：先报 plan_missing
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert _err(r)["code"] == "plan_missing"

    # 手工塞一个“伪可行规划产物”绑定当前哈希，触发 post-check 分支
    p = client.get(f"/api/proposals/{pid}").json()
    h = p["current_snapshot_hash"]
    with Session(main.engine) as s:
        s.add(FmProposalArtifact(
            proposal_id=pid, revision=1, kind="plan", mode="guard_only",
            input_snapshot_hash=h, post_check_ok=True,
            payload={"feasible": True, "mode": "guard_only",
                     "assignments": [{"name": c["name"],
                                      "center_mhz": c["center_mhz"],
                                      "bandwidth_mhz": c["bandwidth_mhz"],
                                      "power_dbm": c["power_dbm"],
                                      "polarization": c["polarization"],
                                      "mask_name": c["mask_name"]}
                                     for c in draft["carriers"]]}))
        s.commit()
    r = client.post(f"/api/proposals/{pid}/apply", json={})
    assert r.status_code == 409
    assert _err(r)["code"] == "post_check_failed"
    # 场景未动
    assert client.get("/api/scenarios/1").json()["version"] == 0


def test_direct_analysis_behavior_unchanged(client):
    """现有场景的直接分析/规划无状态接口不受提案影响。"""
    sc = client.get("/api/scenarios/1").json()
    body = {"carriers": sc["carriers"],
            "rules": {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
                      "reuse_policy": sc["reuse_policy"]},
            "plot_grid_mhz": 0.05}
    res = client.post("/api/analyze", json=body).json()
    pairs = {(f["carrier_a"], f["carrier_b"], f["type"]) for f in res["findings"]}
    assert ("C9", "C10", "overlap") in pairs
    assert ("C1", "C2", "mask_tail") in pairs


def test_diff_carrier_mask_policy_guard(client):
    """差异记录覆盖载波移动、掩模、极化规则、保护间隔四类。"""
    base = _baseline(client)
    draft = copy.deepcopy(base)
    draft["carriers"] = [dict(c) for c in draft["carriers"]]
    c2 = next(c for c in draft["carriers"] if c["name"] == "C2")
    c2["mask_name"] = "strict"
    draft["guard_required_mhz"] = 2.0
    draft["reuse_policy"] = {**draft["reuse_policy"], "H|V": "forbidden"}
    p = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "多维差异", "content": draft}).json()
    diff = p["diff_base"]
    assert any(m["carrier"] == "C2" and m["from"] == "loose" and m["to"] == "strict"
               for m in diff["mask_changes"])
    assert any(x["pair"] == "H|V" for x in diff["polarization_changes"])
    fields = {x["field"] for x in diff["rule_changes"]}
    assert "guard_required_mhz" in fields
    # 即使 C2 没移动也应记录其掩模变化
    assert any(ch["carrier"] == "C2" for ch in diff["carrier_changes"])
