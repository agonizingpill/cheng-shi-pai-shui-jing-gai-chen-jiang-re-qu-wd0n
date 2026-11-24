from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from .database import Base

# 业务证据视图会对这些 JSON 列做 jsonb 运算，Postgres 下使用 JSONB，其它方言回落到 JSON
JsonDoc = JSON().with_variant(JSONB(), "postgresql")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(60), unique=True, nullable=False)
    display_name = Column(String(80), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

class ManholeAsset(Base):
    __tablename__ = "manhole_assets"
    id = Column(Integer, primary_key=True)
    asset_code = Column(String(60), unique=True, nullable=False)
    street_name = Column(String(120), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    coordinate_reference = Column(String(40), nullable=False)
    cover_type = Column(String(40), nullable=False)
    depth_mm = Column(Float, nullable=False)
    status = Column(String(30), nullable=False, default="active")
    last_inspected_at = Column(DateTime(timezone=True), nullable=False)

class SurveyBatch(Base):
    __tablename__ = "survey_batches"
    id = Column(Integer, primary_key=True)
    batch_code = Column(String(60), unique=True, nullable=False)
    measured_at = Column(DateTime(timezone=True), nullable=False)
    instrument = Column(String(100), nullable=False)
    coordinate_reference = Column(String(40), nullable=False)
    status = Column(String(30), nullable=False, default="open")
    notes = Column(Text, nullable=True)
    # 道路预警警戒线，按批次可调；默认 12.0 mm
    warning_limit_mm = Column(Float, nullable=False, default=12.0, server_default="12.0")
    # 测量误差默认值，测点未单列 tolerance_mm 时使用
    default_tolerance_mm = Column(Float, nullable=False, default=0.5, server_default="0.5")
    # 沉降判定版本、订正后是否待重算、当前判定状态
    settlement_revision = Column(Integer, nullable=False, default=0, server_default="0")
    settlement_dirty = Column(Boolean, nullable=False, default=True, server_default="true")
    settlement_state = Column(String(30), nullable=False, default="pending", server_default="'pending'")
    points = relationship("SurveyPoint", back_populates="batch", cascade="all, delete-orphan")

class SurveyPoint(Base):
    __tablename__ = "survey_points"
    id = Column(Integer, primary_key=True)
    point_code = Column(String(60), unique=True, nullable=False)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    manhole_id = Column(Integer, ForeignKey("manhole_assets.id"), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    elevation_mm = Column(Float, nullable=False)
    settlement_mm = Column(Float, nullable=False)
    # 测点级沉降上下限与测量误差（mm）；为空时回落到批次警戒线与默认误差
    limit_lower_mm = Column(Float, nullable=True)
    limit_upper_mm = Column(Float, nullable=True)
    tolerance_mm = Column(Float, nullable=True)
    coordinate_reference = Column(String(40), nullable=False)
    measured_at = Column(DateTime(timezone=True), nullable=False)
    quality_status = Column(String(30), nullable=False, default="unchecked")
    cluster_label = Column(Integer, nullable=True)
    local_density = Column(Float, nullable=True)
    batch = relationship("SurveyBatch", back_populates="points")
    manhole = relationship("ManholeAsset")
    __table_args__ = (UniqueConstraint("batch_id", "manhole_id", name="uq_survey_batch_manhole"),)

class RepairZone(Base):
    __tablename__ = "repair_zones"
    id = Column(Integer, primary_key=True)
    zone_code = Column(String(60), unique=True, nullable=False)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    cluster_label = Column(Integer, nullable=False)
    name = Column(String(120), nullable=False)
    status = Column(String(30), nullable=False, default="proposed")
    point_ids = Column(JSON, nullable=False, default=list)
    center_x = Column(Float, nullable=False)
    center_y = Column(Float, nullable=False)
    average_settlement_mm = Column(Float, nullable=False)
    max_settlement_mm = Column(Float, nullable=False)
    local_density = Column(Float, nullable=False)
    boundary = Column(JSON, nullable=False, default=dict)
    original_cluster_evidence = Column(JSON, nullable=False, default=dict)
    split_from_id = Column(Integer, ForeignKey("repair_zones.id"), nullable=True)
    conflict_notice_id = Column(Integer, ForeignKey("work_notices.id"), nullable=True)
    split_reason = Column(Text, nullable=True)

class WorkNotice(Base):
    __tablename__ = "work_notices"
    id = Column(Integer, primary_key=True)
    notice_code = Column(String(60), unique=True, nullable=False)
    zone_id = Column(Integer, ForeignKey("repair_zones.id"), nullable=True)
    road_name = Column(String(120), nullable=False)
    contractor = Column(String(120), nullable=False)
    permit_no = Column(String(80), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    boundary = Column(JSON, nullable=False, default=dict)
    status = Column(String(30), nullable=False, default="draft")
    lane_closure_required = Column(Boolean, nullable=False, default=False)
    conflict_note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)

class Resurvey(Base):
    __tablename__ = "resurveys"
    id = Column(Integer, primary_key=True)
    record_code = Column(String(60), unique=True, nullable=False)
    zone_id = Column(Integer, ForeignKey("repair_zones.id"), nullable=False)
    measured_at = Column(DateTime(timezone=True), nullable=False)
    settlement_before_mm = Column(Float, nullable=False)
    settlement_after_mm = Column(Float, nullable=False)
    delta_mm = Column(Float, nullable=False)
    pass_fail = Column(String(20), nullable=False)
    status = Column(String(30), nullable=False, default="submitted")
    verified_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)

class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id = Column(Integer, primary_key=True)
    job_code = Column(String(60), unique=True, nullable=False)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    status = Column(String(30), nullable=False, default="queued")
    input_summary = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class SettlementRun(Base):
    """一次沉降超限判定（重算）版本，保留每次签字/订正前后的证据。"""
    __tablename__ = "settlement_runs"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    trigger = Column(String(40), nullable=False)
    status = Column(String(30), nullable=False, default="computed")
    warning_limit_mm = Column(Float, nullable=False)
    default_tolerance_mm = Column(Float, nullable=False)
    scale_px_per_m = Column(Float, nullable=False)
    assessments = Column(JsonDoc, nullable=False, default=list)
    summary = Column(JsonDoc, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    computed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("batch_id", "revision", name="uq_settlement_run_revision"),)


class SettlementPlate(Base):
    """按原始坐标尺度输出的成片；必超限与可能超限各一张。"""
    __tablename__ = "settlement_plates"
    id = Column(Integer, primary_key=True)
    plate_code = Column(String(80), unique=True, nullable=False)
    run_id = Column(Integer, ForeignKey("settlement_runs.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    category = Column(String(30), nullable=False)
    roads = Column(JsonDoc, nullable=False, default=list)
    point_ids = Column(JsonDoc, nullable=False, default=list)
    point_codes = Column(JsonDoc, nullable=False, default=list)
    point_count = Column(Integer, nullable=False, default=0)
    max_settlement_mm = Column(Float, nullable=True)
    min_settlement_mm = Column(Float, nullable=True)
    bounds = Column(JsonDoc, nullable=False, default=dict)
    scale_px_per_m = Column(Float, nullable=False)
    canvas = Column(JsonDoc, nullable=False, default=dict)
    image_svg = Column(Text, nullable=False, default="")
    status = Column(String(30), nullable=False, default="draft")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("run_id", "category", name="uq_settlement_plate_category"),)


class RoadWarning(Base):
    """必超限成片签发的道路预警；每批次一张，重算不覆盖已签发原单。"""
    __tablename__ = "road_warnings"
    id = Column(Integer, primary_key=True)
    warning_code = Column(String(80), unique=True, nullable=False)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    plate_id = Column(Integer, ForeignKey("settlement_plates.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("settlement_runs.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    road_name = Column(String(200), nullable=False)
    status = Column(String(30), nullable=False, default="pending_signoff")
    scope = Column(JsonDoc, nullable=False, default=dict)
    basis = Column(JsonDoc, nullable=False, default=dict)
    signed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("batch_id", name="uq_road_warning_batch"),)


class WarningCorrection(Base):
    """签发后重算范围变化时下发的更正单；按预警 + 重算版本幂等。"""
    __tablename__ = "warning_corrections"
    id = Column(Integer, primary_key=True)
    correction_code = Column(String(90), unique=True, nullable=False)
    warning_id = Column(Integer, ForeignKey("road_warnings.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("survey_batches.id"), nullable=False)
    new_run_id = Column(Integer, ForeignKey("settlement_runs.id"), nullable=False)
    new_plate_id = Column(Integer, ForeignKey("settlement_plates.id"), nullable=True)
    base_revision = Column(Integer, nullable=False)
    new_revision = Column(Integer, nullable=False)
    change_type = Column(String(30), nullable=False)
    scope_before = Column(JsonDoc, nullable=False, default=dict)
    scope_after = Column(JsonDoc, nullable=False, default=dict)
    added_point_codes = Column(JsonDoc, nullable=False, default=list)
    removed_point_codes = Column(JsonDoc, nullable=False, default=list)
    basis = Column(JsonDoc, nullable=False, default=dict)
    reason = Column(Text, nullable=True)
    idempotency_key = Column(String(140), unique=True, nullable=True)
    status = Column(String(30), nullable=False, default="issued")
    issued_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    issued_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    signed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("warning_id", "new_revision", name="uq_warning_correction_revision"),)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    actor_id = Column(Integer, nullable=True)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(80), nullable=False)
    entity_id = Column(String(80), nullable=True)
    detail = Column(JsonDoc, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
