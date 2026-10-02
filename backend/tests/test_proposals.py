"""版本化调频提案的验收测试（内存 SQLite，无需外部服务）。

覆盖：
1) 含多条频率移动的提案 -> 分析 -> 掩模感知规划 -> 评审 -> 应用，新场景通过完整分析；
2) 创建后基准被直接修改，应用被乐观并发拦截，基准不发生部分写入；
3) 草稿改动后旧规划哈希失效，必须重新分析/规划/评审才能应用；
4) 重复应用/取消/回退幂等；刷新与导出可见原基准、应用版本与每次决定的依据。
另含快照哈希与差异纯函数的单测，以及 post-check 失败整体拒绝。
"""
import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base, CarrierRow, MaskRow, Scenario
from app.seed import DEMO_CARRIERS, DEMO_POLICY
from app.services.masks import MASKS
from app.services.snapshots import canonicalize, content_hash, diff_content


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
                      band_high_mhz=220, guard_required_mhz=1.0,
                      leakage_limit_dbm=-45.0, reuse_policy=DEMO_POLICY,
                      carriers=[CarrierRow(**kw) for kw in DEMO_CARRIERS])
        s.add(sc)
        s.commit()
    monkeypatch.setattr(main, "engine", engine)
    with TestClient(main.app) as c:
        yield c


def _demo_content(client, sid=1, **overrides):
    sc = client.get(f"/api/scenarios/{sid}").json()
    content = {
        "name": sc["name"], "description": sc["description"],
        "band_low_mhz": sc["band_low_mhz"], "band_high_mhz": 300.0,
        "guard_required_mhz": sc["guard_required_mhz"],
        "leakage_limit_dbm": sc["leakage_limit_dbm"],
        "reuse_policy": sc["reuse_policy"],
        "carriers": [{k: c[k] for k in
                      ("name", "center_mhz", "bandwidth_mhz", "power_dbm",
                       "polarization", "mask_name")}
                     for c in sc["carriers"]],
    }
    content.update(overrides)
    return content


def _move(content, name, center):
    for c in content["carriers"]:
        if c["name"] == name:
            c["center_mhz"] = center
    return content


def _full_flow(client, moves=("C2", "C5", "C8", "C10"), mode="mask_aware"):
    """创建含多条频率移动的提案，走到评审完成（未应用）。"""
    content = _demo_content(client)
    for i, name in enumerate(moves):
        _move(content, name, content_carrier(content, name) + 6 + 2 * i)
    r = client.post("/api/proposals", json={
        "scenario_id": 1, "title": "拉开冲突载波",
        "rationale": "保护带不足且尾部越界，统一重新排频",
        "content": content, "actor": "学生甲"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "draft"
    assert p["base_revision"] == 0
    assert p["current_revision"] == 1
    assert len(p["diff_vs_base"]["carrier_moves"]) == len(moves)

    assert client.post(f"/api/proposals/{p['id']}/analyze",
                       json={"actor": "学生甲"}).status_code == 200
    r = client.post(f"/api/proposals/{p['id']}/plan",
                    json={"mode": mode, "actor": "学生甲"})
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["result"]["feasible"]
    r = client.post(f"/api/proposals/{p['id']}/review",
                    json={"note": "方案合理", "actor": "王老师"})
    assert r.status_code == 200
    assert r.json()["status"] == "reviewed"
    return p["id"]


def content_carrier(content, name):
    return next(c["center_mhz"] for c in content["carriers"] if c["name"] == name)


# ---- 纯函数：哈希归一化与差异维度 -------------------------------------------

def test_hash_stable_across_float_noise_and_key_order():
    a = {"name": "s", "description": "", "band_low_mhz": 80,
         "band_high_mhz": 220, "guard_required_mhz": 1.0,
         "leakage_limit_dbm": -45.0, "reuse_policy": {"H|V": "unknown"},
         "carriers": [{"name": "C1", "center_mhz": 100.0000001,
                       "bandwidth_mhz": 4.0, "power_dbm": 30.0,
                       "polarization": "H", "mask_name": "loose"}]}
    b = copy.deepcopy(a)
    b["carriers"][0]["center_mhz"] = 100.0
    b["reuse_policy"] = {"H|V": "unknown"}
    assert content_hash(a) == content_hash(b)
    c = copy.deepcopy(b)
    c["carriers"][0]["center_mhz"] = 100.5
    assert content_hash(b) != content_hash(c)


def test_diff_covers_all_four_required_dimensions():
    base = canonicalize(_service_demo())
    draft = copy.deepcopy(base)
    draft["carriers"][1]["center_mhz"] += 5.0          # 频率移动
    draft["carriers"][0]["mask_name"] = "strict"       # 掩模
    draft["reuse_policy"]["H|V"] = "allowed"           # 极化规则
    draft["guard_required_mhz"] = 2.0                  # 保护间隔
    d = diff_content(base, draft)
    assert d["changed"]
    assert d["carrier_moves"][0]["carrier"] == "C2"
    assert d["mask_changes"][0]["carrier"] == "C1"
    assert d["polarization_rule_changes"][0]["pair"] == "H|V"
    assert d["rule_changes"]["guard_required_mhz"] == {"from": 1.0, "to": 2.0}
    assert "频率移动" in d["summary"]
    assert not diff_content(base, canonicalize(copy.deepcopy(base)))["changed"]


def _service_demo():
    return {
        "name": "教学演示场景", "description": "", "band_low_mhz": 80,
        "band_high_mhz": 300, "guard_required_mhz": 1.0,
        "leakage_limit_dbm": -45.0, "reuse_policy": dict(DEMO_POLICY),
        "carriers": [{k: kw[k] for k in
                      ("name", "center_mhz", "bandwidth_mhz", "power_dbm",
                       "polarization", "mask_name")}
                     for kw in DEMO_CARRIERS],
    }


# ---- 验收 1：多移动提案评审后应用，新场景通过完整分析 ------------------------

def test_acceptance_1_create_review_apply_and_new_baseline_analyzes_clean(client):
    pid = _full_flow(client)
    before = client.get("/api/scenarios/1").json()
    assert before["current_revision"] == 0

    r = client.post(f"/api/proposals/{pid}/apply",
                    json={"note": "课后定稿", "actor": "王老师"})
    assert r.status_code == 200, r.text
    applied = r.json()
    assert applied["status"] == "applied"
    assert applied["applied_revision"] == 1

    # 基准已更新且修订号前进；新场景能独立通过 /api/analyze 完整分析
    sc = client.get("/api/scenarios/1").json()
    assert sc["current_revision"] == 1
    body = {"carriers": sc["carriers"],
            "rules": {"guard_required_mhz": sc["guard_required_mhz"],
                      "leakage_limit_dbm": sc["leakage_limit_dbm"],
                      "reuse_policy": sc["reuse_policy"]},
            "plot_grid_mhz": 0.05}
    res = client.post("/api/analyze", json=body).json()
    assert res["counts"]["error"] == 0, res["findings"]

    # 与原基准相比确实有多条频率移动（不是空应用）
    base_centers = {c["name"]: c["center_mhz"] for c in before["carriers"]}
    moved = [c for c in sc["carriers"]
             if c["center_mhz"] != base_centers[c["name"]]]
    assert len(moved) >= 2

    # 版本链：revision 0 原基准 + revision 1 应用版本，内容都可查看
    versions = client.get("/api/scenarios/1/versions").json()
    assert [(v["revision"], v["kind"]) for v in versions] == [(0, "baseline"),
                                                              (1, "applied")]
    v1 = client.get("/api/scenarios/1/versions/1").json()
    assert v1["proposal_id"] == pid
    assert len(v1["content"]["carriers"]) == len(sc["carriers"])


# ---- 验收 2：基准被他人改动 -> 冲突拦截，基准不变 ----------------------------

def test_acceptance_2_baseline_modified_blocks_apply_without_partial_write(client):
    pid = _full_flow(client)
    # 他人直接修改基准（C1 功率 30 -> 31），基准修订号前进
    baseline = client.get("/api/scenarios/1").json()
    payload = {k: baseline[k] for k in
               ("name", "description", "band_low_mhz", "band_high_mhz",
                "guard_required_mhz", "leakage_limit_dbm", "reuse_policy")}
    payload["carriers"] = [{k: c[k] for k in
                            ("name", "center_mhz", "bandwidth_mhz",
                             "power_dbm", "polarization", "mask_name")}
                           for c in baseline["carriers"]]
    payload["carriers"][0]["power_dbm"] = 31.0
    assert client.put("/api/scenarios/1", json=payload).status_code == 200
    modified = client.get("/api/scenarios/1").json()
    assert modified["current_revision"] == 1

    r = client.post(f"/api/proposals/{pid}/apply", json={"actor": "王老师"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "baseline_conflict"

    # 基准保持为“被他人修改后”的内容：没有任何频率被部分写入，修订号不变
    after = client.get("/api/scenarios/1").json()
    assert after["current_revision"] == 1
    assert after["carriers"][0]["power_dbm"] == 31.0
    before_centers = {c["name"]: c["center_mhz"] for c in modified["carriers"]}
    after_centers = {c["name"]: c["center_mhz"] for c in after["carriers"]}
    assert before_centers == after_centers

    # 提案仍为 reviewed，拒审原因进入只追加审计
    p = client.get(f"/api/proposals/{pid}").json()
    assert p["status"] == "reviewed"
    assert any(e["type"] == "apply_rejected"
               and e["detail"]["reason"] == "baseline_conflict"
               for e in p["events"])


# ---- 验收 3：草稿改动使旧规划哈希失效，必须重新规划 --------------------------

def test_acceptance_3_stale_plan_after_draft_edit_requires_replan(client):
    pid = _full_flow(client)

    # 教师改动草稿（追加一条移动）-> 修订号 2，状态退回 draft，分析/规划全部失效
    p = client.get(f"/api/proposals/{pid}").json()
    content = p["latest_snapshot"]["content"]
    old_plan_hash = p["plan"]["input_hash"]
    _move(content, "C3", 120.0)
    r = client.put(f"/api/proposals/{pid}/draft",
                   json={"content": content, "note": "再动 C3", "actor": "王老师"})
    assert r.status_code == 200
    p2 = r.json()
    assert p2["current_revision"] == 2
    assert p2["status"] == "draft"
    assert p2["analysis"] is None and p2["plan"] is None
    assert p2["current_hash"] != old_plan_hash

    # 只重新分析并评审（不重新规划）：应用时明确报 plan_stale，而不是悄悄使用旧方案
    client.post(f"/api/proposals/{pid}/analyze", json={"actor": "王老师"})
    client.post(f"/api/proposals/{pid}/review", json={"actor": "王老师"})
    r = client.post(f"/api/proposals/{pid}/apply", json={"actor": "王老师"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "plan_stale"
    # 被拒后基准仍然纹丝不动
    assert client.get("/api/scenarios/1").json()["current_revision"] == 0

    # 重新规划 -> 重新评审 -> 应用成功
    assert client.post(f"/api/proposals/{pid}/plan",
                       json={"mode": "mask_aware", "actor": "王老师"}).status_code == 200
    assert client.post(f"/api/proposals/{pid}/review",
                       json={"actor": "王老师"}).status_code == 200
    r = client.post(f"/api/proposals/{pid}/apply", json={"actor": "王老师"})
    assert r.status_code == 200
    assert r.json()["status"] == "applied"


def test_review_requires_current_analysis(client):
    pid = _create_simple_proposal(client)
    # 没有当前快照分析直接评审 -> 拒绝
    r = client.post(f"/api/proposals/{pid}/review", json={"actor": "王老师"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "analysis_missing"


def _create_simple_proposal(client, content=None):
    content = content or _demo_content(client)
    r = client.post("/api/proposals",
                    json={"scenario_id": 1, "title": "t", "content": content})
    return r.json()["id"]


# ---- 验收 4：重复应用/取消/回退幂等 + 导出与刷新可见全部依据 -----------------

def test_acceptance_4_idempotent_decisions_and_exportable_history(client):
    pid = _full_flow(client)
    apply1 = client.post(f"/api/proposals/{pid}/apply",
                         json={"actor": "王老师"}).json()
    assert apply1["applied_revision"] == 1

    # 重复应用：幂等，审计里只有一条 applied
    apply2 = client.post(f"/api/proposals/{pid}/apply",
                         json={"actor": "王老师"}).json()
    assert apply2["status"] == "applied"
    assert apply2["applied_revision"] == 1
    p = client.get(f"/api/proposals/{pid}").json()  # “刷新”
    assert p["status"] == "applied"
    assert [e["type"] for e in p["events"]].count("applied") == 1
    # 已应用后不能取消
    assert client.post(f"/api/proposals/{pid}/cancel",
                       json={"actor": "王老师"}).status_code == 409

    # 回退两次都成功（第二次幂等），只有一条 rolled_back；恢复成原基准内容
    rb1 = client.post(f"/api/proposals/{pid}/rollback",
                      json={"note": "下节课再讲，先恢复", "actor": "王老师"})
    assert rb1.status_code == 200
    assert rb1.json()["status"] == "rolled_back"
    rb2 = client.post(f"/api/proposals/{pid}/rollback",
                      json={"actor": "王老师"})
    assert rb2.status_code == 200 and rb2.json()["status"] == "rolled_back"
    p = client.get(f"/api/proposals/{pid}").json()
    assert [e["type"] for e in p["events"]].count("rolled_back") == 1

    sc = client.get("/api/scenarios/1").json()
    # 回退生成新版本 revision 2，内容哈希与原基准 revision 0 一致
    versions = client.get("/api/scenarios/1/versions").json()
    assert [v["revision"] for v in versions] == [0, 1, 2]
    v0, v1, v2 = (client.get(f"/api/scenarios/1/versions/{r}").json()
                  for r in (0, 1, 2))
    assert v2["content_hash"] == v0["content_hash"]
    assert v1["kind"] == "applied" and v2["kind"] == "rollback"
    live = client.get("/api/scenarios/1").json()
    assert [c["center_mhz"] for c in live["carriers"]] == \
           [c["center_mhz"] for c in v0["content"]["carriers"]]

    # 回退后不能再应用/再回退之外的非法跃迁
    assert client.post(f"/api/proposals/{pid}/cancel",
                       json={"actor": "王老师"}).status_code == 409

    # 导出：原基准 + 已应用版本 + 每次决定的依据（哈希绑定）+ 可重放流水
    export = client.get(f"/api/proposals/{pid}/export").json()
    assert export["baseline_snapshot"]["content_hash"] == p["base_hash"]
    kinds = {v["kind"] for v in export["scenario_versions"]}
    assert {"baseline", "applied", "rollback"} <= kinds
    applied_version = next(v for v in export["scenario_versions"]
                           if v["kind"] == "applied")
    assert applied_version["proposal_id"] == pid
    event_types = [e["type"] for e in export["events"]]
    assert {"created", "analysis_run", "plan_run", "reviewed", "applied",
            "rolled_back"} <= set(event_types)
    # 每个分析/规划产物都带输入哈希，可核对“决定的依据”
    for a in export["artifacts"]:
        assert len(a["input_hash"]) == 64
        assert a["revision"] >= 1
    assert len(export["replay"]) == len(export["events"])


def test_cancel_is_idempotent_and_leaves_baseline_untouched(client):
    pid = _create_simple_proposal(client)
    r1 = client.post(f"/api/proposals/{pid}/cancel",
                     json={"note": "方向错了", "actor": "王老师"})
    r2 = client.post(f"/api/proposals/{pid}/cancel", json={"actor": "王老师"})
    assert r1.status_code == r2.status_code == 200
    p = client.get(f"/api/proposals/{pid}").json()
    assert p["status"] == "cancelled"
    assert [e["type"] for e in p["events"]].count("cancelled") == 1
    # 取消不影响基准：没有任何应用版本
    versions = client.get("/api/scenarios/1/versions").json()
    assert [v["kind"] for v in versions] == ["baseline"]
    # 取消后不能评审/应用
    assert client.post(f"/api/proposals/{pid}/analyze",
                       json={"actor": "x"}).status_code == 409
    assert client.post(f"/api/proposals/{pid}/apply",
                       json={"actor": "王老师"}).status_code == 409


# ---- post-check 失败整体拒绝（仅保护间隔规划留得住掩模尾部冲突） -------------

def test_guard_only_plan_with_mask_errors_is_rejected_atomically(client):
    # 手工草稿：只把几何间隔排开到恰好满足保护带，但 loose 掩模拖尾仍越界
    content = _demo_content(client)
    _move(content, "C1", 100.0)
    _move(content, "C2", 105.0)   # 边缘净距 1.0，guard 达标、loose 拖尾仍越界
    # 屏蔽其余载波干扰，只保留这一对
    content["carriers"] = [c for c in content["carriers"]
                           if c["name"] in ("C1", "C2")]
    pid = _create_simple_proposal(client, content)
    client.post(f"/api/proposals/{pid}/analyze", json={"actor": "学生甲"})
    r = client.post(f"/api/proposals/{pid}/plan",
                    json={"mode": "guard_only", "actor": "学生甲"}).json()
    assert r["plan"]["result"]["feasible"]
    assert r["plan"]["result"]["post_check"]["counts"]["error"] >= 1
    client.post(f"/api/proposals/{pid}/review", json={"actor": "王老师"})

    res = client.post(f"/api/proposals/{pid}/apply", json={"actor": "王老师"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "post_check_failed"
    # 基准完全没动
    sc = client.get("/api/scenarios/1").json()
    assert sc["current_revision"] == 0
    assert content_carrier(
        _demo_content(client), "C1") == 100.0


def test_direct_scenario_edit_appends_baseline_version_and_noop_dedup(client):
    sc = client.get("/api/scenarios/1").json()
    payload = {k: sc[k] for k in
               ("name", "description", "band_low_mhz", "band_high_mhz",
                "guard_required_mhz", "leakage_limit_dbm", "reuse_policy")}
    payload["carriers"] = [{k: c[k] for k in
                            ("name", "center_mhz", "bandwidth_mhz",
                             "power_dbm", "polarization", "mask_name")}
                           for c in sc["carriers"]]
    # 内容没变 -> 不产生新版本
    client.put("/api/scenarios/1", json=payload)
    assert client.get("/api/scenarios/1").json()["current_revision"] == 0
    # 真正改动 -> 追加 baseline 版本
    payload["guard_required_mhz"] = 1.5
    client.put("/api/scenarios/1", json=payload)
    assert client.get("/api/scenarios/1").json()["current_revision"] == 1
    versions = client.get("/api/scenarios/1/versions").json()
    assert versions[-1]["kind"] == "baseline"


def test_proposal_list_freshness_flags(client):
    pid = _full_flow(client)
    rows = client.get("/api/proposals").json()
    row = next(r for r in rows if r["id"] == pid)
    assert row["status"] == "reviewed"
    assert row["plan_fresh"] is True
    assert row["diff_summary"]["moves"] >= 2
    assert row["base_hash"] and len(row["base_hash"]) == 64


def test_concurrent_proposals_second_apply_is_conficted(client):
    pid1 = _full_flow(client)
    pid2 = _full_flow(client)
    # 第一个提案应用成功
    assert client.post(f"/api/proposals/{pid1}/apply",
                       json={"actor": "王老师"}).status_code == 200
    # 第二个提案仍基于 revision 0：应用被冲突拦截，且不覆盖刚落地的 r1
    r = client.post(f"/api/proposals/{pid2}/apply", json={"actor": "王老师"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "baseline_conflict"
    sc = client.get("/api/scenarios/1").json()
    assert sc["current_revision"] == 1
    p2 = client.get(f"/api/proposals/{pid2}").json()
    assert p2["status"] == "reviewed"
    assert any(e["type"] == "apply_rejected" for e in p2["events"])
    # 回退第一个提案后，第二个提案仍指向 r0 基线（revision 已到 2），依旧被拦截
    assert client.post(f"/api/proposals/{pid1}/rollback",
                       json={"actor": "王老师"}).status_code == 200
    r = client.post(f"/api/proposals/{pid2}/apply", json={"actor": "王老师"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "baseline_conflict"


def test_rollback_before_applied_and_illegal_transitions(client):
    pid = _create_simple_proposal(client)
    # 草稿态回退 -> 409
    r = client.post(f"/api/proposals/{pid}/rollback", json={"actor": "王老师"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "proposal_draft"
    # 评审后取消，再评审/回退/应用都是终态冲突
    client.post(f"/api/proposals/{pid}/analyze", json={"actor": "x"})
    client.post(f"/api/proposals/{pid}/review", json={"actor": "王老师"})
    client.post(f"/api/proposals/{pid}/cancel", json={"actor": "王老师"})
    for action in ("review", "rollback"):
        r = client.post(f"/api/proposals/{pid}/{action}", json={"actor": "王老师"})
        assert r.status_code == 409, action


def test_unknown_scenario_and_proposal_404(client):
    r = client.post("/api/proposals",
                    json={"scenario_id": 999, "title": "x"})
    assert r.status_code == 404
    assert client.get("/api/proposals/999").status_code == 404
    assert client.get("/api/scenarios/999/versions").status_code == 404


def test_invalid_draft_content_rejected(client):
    pid = _create_simple_proposal(client)
    p = client.get(f"/api/proposals/{pid}").json()
    bad = p["latest_snapshot"]["content"]
    bad["carriers"][0]["mask_name"] = "no-such-mask"
    r = client.put(f"/api/proposals/{pid}/draft", json={"content": bad})
    assert r.status_code == 400
    # 失败不留半成品快照
    p2 = client.get(f"/api/proposals/{pid}").json()
    assert p2["current_revision"] == 1


def test_plan_freshness_independent_of_analysis(client):
    # 规划是应用的必要依据；但只有当前分析才能评审
    pid = _create_simple_proposal(client)
    client.post(f"/api/proposals/{pid}/plan", json={"mode": "mask_aware"})
    p = client.get(f"/api/proposals/{pid}").json()
    assert p["plan"] is not None and p["analysis"] is None
    r = client.post(f"/api/proposals/{pid}/review", json={"actor": "王老师"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "analysis_missing"
