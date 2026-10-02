"""SQLAlchemy 模型：场景、载波、示例频谱掩模、版本化调频提案。

版本化相关表：

- ``scenario_versions``   场景（教学基准）每次写入前后的不可变快照
- ``fm_proposals``        调频提案：状态机 draft/reviewed/applied/cancelled/rolled_back
- ``fm_proposal_revisions`` 提案每次内容修订（差异相对上一修订，首修相对基准）
- ``fm_proposal_artifacts`` 分析/规划结果，绑定输入快照哈希 + 修订号
- ``fm_proposal_events``  审计时间线（可重放历史）
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    # 乐观并发版本号：任何直接写入（PUT/DELETE 语义的更新）与提案应用都会 +1
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    carriers: Mapped[list["CarrierRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="CarrierRow.position")


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


# ---- 版本化调频提案 ---------------------------------------------------------


class ScenarioVersion(Base):
    """场景内容的不可变版本快照（版本号自 0 起）。

    kind:
      baseline       初始/直接编辑产生的教学基准版本
      proposal_applied  由调频提案应用写入
      rollback       由已应用提案回退写入
    """
    __tablename__ = "scenario_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="baseline")
    # 规范化快照（与 proposals.snapshot 同一格式）与内容哈希
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 产生该版本的提案/事件（直接编辑为 None）
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("fm_proposals.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    __table_args__ = (
        UniqueConstraint("scenario_id", "version", name="uq_scenario_version"),
    )


class FmProposal(Base):
    """调频提案：从基准场景创建草稿，经评审后可应用，可取消/回退。"""
    __tablename__ = "fm_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    # 创建时的基准锚点：乐观并发校验依据
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # 应用/回退后记录对应场景版本，便于审计定位
    applied_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rolled_back_to_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(),
                                                 onupdate=func.now())

    revisions: Mapped[list["FmProposalRevision"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan",
        order_by="FmProposalRevision.revision")
    events: Mapped[list["FmProposalEvent"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan",
        order_by="FmProposalEvent.sequence")


class FmProposalRevision(Base):
    """提案内容修订（不可变）。rev=1 为创建时的草稿快照。"""
    __tablename__ = "fm_proposal_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("fm_proposals.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 相对上一修订（rev=1 时相对基准快照）的结构化差异
    diff: Mapped[dict] = mapped_column(JSON, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    proposal: Mapped[FmProposal] = relationship(back_populates="revisions")

    __table_args__ = (
        UniqueConstraint("proposal_id", "revision", name="uq_proposal_revision"),
    )


class FmProposalArtifact(Base):
    """分析/规划结果：必须绑定输入快照哈希与修订号。

    修订推进后，旧修订上的产物不再能作为应用依据（plan_expired）。
    """
    __tablename__ = "fm_proposal_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("fm_proposals.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # analysis / plan
    input_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 规划模式（guard_only / mask_aware）；分析产物为 None
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 规划产物的 post_check 汇总结论（是否零冲突），冗余便于列表展示
    post_check_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    __table_args__ = (
        UniqueConstraint("proposal_id", "revision", "kind", "mode",
                         name="uq_proposal_artifact"),
    )


class FmProposalEvent(Base):
    """提案审计时间线（每次决定的依据），顺序追加、不可修改。"""
    __tablename__ = "fm_proposal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("fm_proposals.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    proposal: Mapped[FmProposal] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("proposal_id", "sequence", name="uq_proposal_event_seq"),
    )
