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
    current_revision: int = 0
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
    current_revision: int = 0


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str


# ---- 版本化调频提案 --------------------------------------------------------

class ScenarioContent(BaseModel):
    """提案草稿内容：基准快照的可编辑投影。"""
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float = Field(ge=0)
    leakage_limit_dbm: float
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(
        default_factory=dict)
    carriers: list[CarrierIn] = Field(min_length=1)


class ProposalCreate(BaseModel):
    scenario_id: int
    title: str = Field(min_length=1, max_length=128)
    rationale: str = ""
    # 可选：初始草稿内容；缺省时以当前基准内容建出第 1 版快照
    content: Optional[ScenarioContent] = None
    note: str = ""
    actor: str = "学生"


class ProposalDraftIn(BaseModel):
    content: ScenarioContent
    note: str = ""
    actor: str = "学生"


class ProposalReviewIn(BaseModel):
    note: str = "评审通过"
    actor: str = "教师"


class DecisionIn(BaseModel):
    """应用 / 取消 / 回退的幂等请求体（全部字段可选）。"""
    note: str = ""
    actor: str = "教师"
    # 客户端看到的基准版本号：服务端以当前版本做权威校验，该字段仅用于审计
    expected_base_revision: Optional[int] = None


class ProposalPlanIn(BaseModel):
    mode: Literal["guard_only", "mask_aware"] = "mask_aware"
    actor: str = "学生"


class ActorIn(BaseModel):
    actor: str = "学生"


class ArtifactOut(BaseModel):
    id: int
    kind: str
    revision: int
    input_hash: str
    mode: Optional[str] = None
    actor: str
    created_at: Optional[str] = None
    result: dict


class SnapshotOut(BaseModel):
    id: int
    revision: int
    content_hash: str
    content: dict
    diff_vs_base: dict
    note: str
    created_at: Optional[str] = None


class EventOut(BaseModel):
    id: int
    seq: int
    type: str
    actor: str
    note: str
    detail: dict
    created_at: Optional[str] = None


class ScenarioVersionOut(BaseModel):
    id: int
    revision: int
    kind: str
    label: str
    content_hash: str
    proposal_id: Optional[int] = None
    actor: str
    created_at: Optional[str] = None


class ScenarioVersionDetail(ScenarioVersionOut):
    content: dict


class ProposalOut(BaseModel):
    id: int
    scenario_id: int
    title: str
    rationale: str
    status: str
    base_revision: int
    base_hash: str
    applied_revision: Optional[int] = None
    created_by: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    applied_at: Optional[str] = None
    current_revision: int
    current_hash: str
    diff_vs_base: dict
    latest_snapshot: Optional[SnapshotOut] = None
    analysis: Optional[ArtifactOut] = None
    plan: Optional[ArtifactOut] = None
    events: list[EventOut] = Field(default_factory=list)


class ProposalSummary(BaseModel):
    id: int
    scenario_id: int
    title: str
    status: str
    base_revision: int
    base_hash: str
    current_revision: int
    current_hash: str
    applied_revision: Optional[int] = None
    diff_summary: dict
    plan_fresh: bool
    has_analysis: bool
    created_by: str
    created_at: Optional[str] = None
