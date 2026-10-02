"""SQLAlchemy 模型：场景、载波、示例频谱掩模、版本化调频提案。

提案相关表之间的关系：

- scenarios.current_revision 是教学基准的乐观并发令牌（每次直接编辑/应用/回退 +1）。
- scenario_versions 是基准侧不可变快照链（baseline / applied / rollback）。
- proposals 携带创建时基准快照（base_content/base_hash/base_revision）；
  proposal_snapshots 是草稿每次修订的不可变快照 + 相对基准的结构化差异；
  proposal_artifacts 是分析/规划结果，必须绑定输入快照哈希与草稿修订号；
  proposal_events 是只追加的决定/拒审审计流水，支持回放。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, func)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship)


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    band_low_mhz: Mapped[float] = mapped_column(Float, default=80.0)
    band_high_mhz: Mapped[float] = mapped_column(Float, default=220.0)
    guard_required_mhz: Mapped[float] = mapped_column(Float, default=1.0)
    leakage_limit_dbm: Mapped[float] = mapped_column(Float, default=-45.0)
    # 极化复用规则，如 {"H|V": "unknown", "RHCP|V": "allowed"}
    reuse_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    # 教学基准当前版本号（乐观并发令牌）：每次基准内容变化 +1
    current_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    carriers: Mapped[list["CarrierRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="CarrierRow.position")
    versions: Mapped[list["ScenarioVersion"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="ScenarioVersion.revision")
    proposals: Mapped[list["Proposal"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="desc(Proposal.id)")


class CarrierRow(Base):
    __tablename__ = "carriers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    center_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    bandwidth_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    power_dbm: Mapped[float] = mapped_column(Float, nullable=False)
    polarization: Mapped[str] = mapped_column(String(8), nullable=False)
    mask_name: Mapped[str] = mapped_column(String(32), nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="carriers")


class MaskRow(Base):
    """示例频谱发射掩模：名称 + 折线点 + 说明（教学示例）。"""
    __tablename__ = "masks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 一侧（非负偏移）折线点 [[offset_mhz, attenuation_db], ...]
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    span_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


# ---- 版本化调频提案 --------------------------------------------------------

PROPOSAL_STATES = ("draft", "reviewed", "applied", "cancelled", "rolled_back")
"""提案状态机：

    draft ──review──▶ reviewed ──apply──▶ applied ──rollback──▶ rolled_back
      ▲                  │                   （重复 apply/rollback 幂等）
      └── 草稿再次修订 ──┘
    draft/reviewed ──cancel──▶ cancelled（重复 cancel 幂等）
"""


class ScenarioVersion(Base):
    """教学基准侧的不可变版本快照（直接编辑 / 提案应用 / 回退各产生一条）。"""
    __tablename__ = "scenario_versions"
    __table_args__ = (UniqueConstraint("scenario_id", "revision",
                                       name="uq_scenario_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # baseline（直接编辑/初始）/ applied（提案应用）/ rollback（回退）
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(256), default="")
    # 完整场景内容（与哈希输入一致）
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    # applied/rollback 时关联到提案；baseline 为 NULL
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL"), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="系统")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    scenario: Mapped[Scenario] = relationship(back_populates="versions")


class Proposal(Base):
    """调频提案：从基准场景拉出的草稿，经评审后才能原子应用。"""
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)

    # 创建时锁定的基准：版本号（乐观并发用）+ 完整快照 + 哈希
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    base_content: Mapped[dict] = mapped_column(JSON, nullable=False)
    base_hash: Mapped[str] = mapped_column(String(80), nullable=False)

    # 应用成功后回填
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="学生")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True)

    scenario: Mapped[Scenario] = relationship(back_populates="proposals")
    snapshots: Mapped[list["ProposalSnapshot"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan",
        order_by="ProposalSnapshot.revision")
    artifacts: Mapped[list["ProposalArtifact"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan",
        order_by="ProposalArtifact.id")
    events: Mapped[list["ProposalEvent"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan",
        order_by="ProposalEvent.seq")


class ProposalSnapshot(Base):
    """草稿的一次不可变修订（revision 从 1 开始），含相对基准的结构化差异。"""
    __tablename__ = "proposal_snapshots"
    __table_args__ = (UniqueConstraint("proposal_id", "revision",
                                       name="uq_proposal_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    # 相对创建时基准的差异（载波移动 / 掩模 / 极化规则 / 保护间隔）
    diff_vs_base: Mapped[dict] = mapped_column(JSON, nullable=False)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    proposal: Mapped[Proposal] = relationship(back_populates="snapshots")


class ProposalArtifact(Base):
    """分析 / 规划结果记录。

    应用时只接受 revision == 提案当前修订号且 input_hash == 当前草稿哈希
    的规划结果，保证“先规划、后改草稿”的旧结果无法落地。
    """
    __tablename__ = "proposal_artifacts"
    __table_args__ = (UniqueConstraint(
        "proposal_id", "kind", "revision", name="uq_proposal_artifact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"))
    # analysis / plan
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    # plan 模式：guard_only / mask_aware；analysis 为 NULL
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), default="学生")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    proposal: Mapped[Proposal] = relationship(back_populates="artifacts")


class ProposalEvent(Base):
    """只追加的审计流水：创建、修订、分析、规划、评审、应用、拒审、取消、回退。"""
    __tablename__ = "proposal_events"
    __table_args__ = (UniqueConstraint("proposal_id", "seq",
                                       name="uq_proposal_event_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # created / updated / reset_to_draft / analysis_run / plan_run /
    # reviewed / applied / apply_rejected / cancelled / rolled_back
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), default="系统")
    note: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    proposal: Mapped[Proposal] = relationship(back_populates="events")
