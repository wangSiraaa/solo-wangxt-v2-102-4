"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

POLARIZATIONS = ("H", "V", "LHCP", "RHCP")
REUSE_VALUES = ("forbidden", "allowed", "unknown")


class CarrierIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    center_mhz: float = Field(gt=0)
    bandwidth_mhz: float = Field(gt=0)
    power_dbm: float
    polarization: Literal["H", "V", "LHCP", "RHCP"]
    mask_name: str = "strict"

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("载波名不能为空")
        return v


class RulesIn(BaseModel):
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    # key 形如 "H|V"（极化名按字母排序后拼接），value forbidden/allowed/unknown
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 绘图网格步长 (MHz)
    plot_grid_mhz: float = Field(default=0.05, gt=0, le=1.0)


class PlanRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    mode: Literal["guard_only", "mask_aware"] = "guard_only"

    def validate_band(self) -> None:
        if self.band_high_mhz <= self.band_low_mhz:
            raise ValueError("band_high_mhz 必须大于 band_low_mhz")


class CarrierOut(BaseModel):
    id: Optional[int] = None
    name: str
    center_mhz: float
    bandwidth_mhz: float
    power_dbm: float
    polarization: str
    mask_name: str


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)
    carriers: list[CarrierIn] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    id: int
    name: str
    description: str
    carrier_count: int
    version: int = 0
    created_at: Optional[str] = None


class ScenarioOut(BaseModel):
    id: int
    name: str
    description: str
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float
    leakage_limit_dbm: float
    reuse_policy: dict[str, str]
    carriers: list[CarrierOut]
    version: int = 0


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str


# ---- 版本化调频提案 --------------------------------------------------------

# 提案内容复用场景内容结构（不含场景名等场景元数据时也合法）
ScenarioContent = ScenarioIn

PROPOSAL_STATUSES = ("draft", "reviewed", "applied", "cancelled", "rolled_back")
ProposalStatus = Literal["draft", "reviewed", "applied", "cancelled", "rolled_back"]


class ProposalCreateIn(BaseModel):
    scenario_id: int
    title: str = Field(min_length=1, max_length=128)
    description: str = ""
    # 草稿内容；为空时复制当前基准（无差异草稿，便于先建后改）
    content: Optional[ScenarioContent] = None
    note: str = ""


class ProposalReviseIn(BaseModel):
    content: ScenarioContent
    note: str = ""


class ProposalDecisionIn(BaseModel):
    note: str = ""


class ProposalPlanIn(BaseModel):
    mode: Literal["guard_only", "mask_aware"] = "mask_aware"


class RevisionOut(BaseModel):
    revision: int
    snapshot_hash: str
    diff: dict
    note: str
    created_by: str
    created_at: Optional[str] = None
    # 当前修订上的产物概要（前端用来判断“规划是否过期”）
    artifacts: list["ArtifactOut"] = Field(default_factory=list)


class ArtifactOut(BaseModel):
    id: int
    revision: int
    kind: Literal["analysis", "plan"]
    mode: Optional[str] = None
    input_snapshot_hash: str
    post_check_ok: bool = False
    payload: dict
    created_at: Optional[str] = None


class EventOut(BaseModel):
    sequence: int
    event_type: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor: str = ""
    note: str = ""
    detail: dict = Field(default_factory=dict)
    created_at: Optional[str] = None


class ProposalSummary(BaseModel):
    id: int
    scenario_id: int
    title: str
    status: ProposalStatus
    base_version: int
    revision_count: int
    applied_version: Optional[int] = None
    rolled_back_to_version: Optional[int] = None
    created_by: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 最新修订相对基准的差异概要
    diff_summary: Optional[dict] = None


class ProposalOut(BaseModel):
    id: int
    scenario_id: int
    title: str
    description: str
    status: ProposalStatus
    base_version: int
    base_snapshot_hash: str
    applied_version: Optional[int] = None
    rolled_back_to_version: Optional[int] = None
    created_by: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 锚定基准快照 + 当前草稿快照 + 累计差异
    base_snapshot: dict
    current_revision: int
    current_snapshot: dict
    current_snapshot_hash: str
    diff_base: dict
    revisions: list[RevisionOut]
    events: list[EventOut]
    # 场景当前版本：前端可提示“基准已被他人修改”
    scenario_current_version: int
    baseline_changed: bool


class ScenarioVersionOut(BaseModel):
    version: int
    kind: str
    snapshot_hash: str
    proposal_id: Optional[int] = None
    note: str = ""
    actor: str = ""
    created_at: Optional[str] = None
    snapshot: Optional[dict] = None
    diff_from_previous: Optional[dict] = None


RevisionOut.model_rebuild()
