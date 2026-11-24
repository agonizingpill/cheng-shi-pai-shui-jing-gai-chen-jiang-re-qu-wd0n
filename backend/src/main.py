import os, json, logging
from datetime import datetime, timezone
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from redis import Redis

from .operations_evidence import router as operations_evidence
from .database import Base, SessionLocal, engine, get_db
from .models import (
    AnalysisJob,
    AuditLog,
    ManholeAsset,
    RepairZone,
    Resurvey,
    RoadWarning,
    SettlementPlate,
    SettlementRun,
    SurveyBatch,
    SurveyPoint,
    User,
    WarningCorrection,
    WorkNotice,
)
from .schemas import (
    BatchIn,
    BatchLimitsIn,
    CorrectionIssueIn,
    LoginIn,
    NoticeIn,
    PointLimitsIn,
    ReadingCorrectionIn,
    ResurveyIn,
    SurveyPointIn,
    WarningSignoffIn,
)
from .security import create_token, current_user, require_roles, verify_password
from .services.audit import record
from .services.cluster import build_input
from .services.geometry import bounds, overlaps
from .services import settlement as settle
from .middlewares.audit import RequestAuditMiddleware
from .middlewares.rate_limit import SimpleRateLimitMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("manhole-settlement")
app = FastAPI(title="城市排水井盖沉降热区", version="1.0.0")
app.include_router(operations_evidence)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(RequestAuditMiddleware)
app.add_middleware(SimpleRateLimitMiddleware)
redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    ensure_settlement_columns()
    if os.getenv("SEED_DEMO", "true").lower() == "true":
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:
            logger.exception("seed_demo failed during startup")
    try:
        with SessionLocal() as db:
            batch = db.execute(select(SurveyBatch).order_by(SurveyBatch.measured_at.desc())).scalars().first()
            if batch and not settle.latest_run(db, batch.id):
                settle.create_run(db, batch, trigger="startup")
                db.commit()
    except Exception:
        logger.exception("initial settlement run failed")

SETTLEMENT_COLUMN_MIGRATIONS = [
    ("survey_batches", "warning_limit_mm", "FLOAT NOT NULL DEFAULT 12.0"),
    ("survey_batches", "default_tolerance_mm", "FLOAT NOT NULL DEFAULT 0.5"),
    ("survey_batches", "settlement_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("survey_batches", "settlement_dirty", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("survey_batches", "settlement_state", "VARCHAR(30) NOT NULL DEFAULT 'pending'"),
    ("survey_points", "limit_lower_mm", "FLOAT"),
    ("survey_points", "limit_upper_mm", "FLOAT"),
    ("survey_points", "tolerance_mm", "FLOAT"),
]

def ensure_settlement_columns():
    """幂等补齐已有库的沉降阈值列，避免旧数据卷缺列；SQLite 仅用于本地测试。"""
    from sqlalchemy import text
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            existing = {
                table: {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
                for table, _, _ in SETTLEMENT_COLUMN_MIGRATIONS
            }
            for table, column, ddl in SETTLEMENT_COLUMN_MIGRATIONS:
                if column not in existing.get(table, set()):
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        else:
            for table, column, ddl in SETTLEMENT_COLUMN_MIGRATIONS:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))

@app.get("/health")
def health():
    return {"status": "ok", "service": "manhole-settlement-api"}

def iso(value):
    return value.isoformat() if value else None

def user_view(user):
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}

def asset_view(a):
    return {"id": a.id, "asset_code": a.asset_code, "street_name": a.street_name, "x": a.x, "y": a.y, "coordinate_reference": a.coordinate_reference, "cover_type": a.cover_type, "depth_mm": a.depth_mm, "status": a.status, "last_inspected_at": iso(a.last_inspected_at)}

def point_view(p):
    return {"id": p.id, "point_code": p.point_code, "batch_id": p.batch_id, "manhole_id": p.manhole_id, "asset_code": p.manhole.asset_code if p.manhole else "", "street_name": p.manhole.street_name if p.manhole else "", "x": p.x, "y": p.y, "elevation_mm": p.elevation_mm, "settlement_mm": p.settlement_mm, "limit_lower_mm": p.limit_lower_mm, "limit_upper_mm": p.limit_upper_mm, "tolerance_mm": p.tolerance_mm, "coordinate_reference": p.coordinate_reference, "measured_at": iso(p.measured_at), "quality_status": p.quality_status, "cluster_label": p.cluster_label, "local_density": p.local_density}

def batch_view(b):
    return {"id": b.id, "batch_code": b.batch_code, "measured_at": iso(b.measured_at), "instrument": b.instrument, "coordinate_reference": b.coordinate_reference, "status": b.status, "notes": b.notes, "warning_limit_mm": b.warning_limit_mm, "default_tolerance_mm": b.default_tolerance_mm, "settlement_revision": b.settlement_revision, "settlement_dirty": bool(b.settlement_dirty), "settlement_state": b.settlement_state, "point_count": len(b.points)}

def zone_view(z, db, overview=None):
    notice = db.get(WorkNotice, z.conflict_notice_id) if z.conflict_notice_id else None
    point_rows = db.execute(select(SurveyPoint).where(SurveyPoint.id.in_(z.point_ids or []))).scalars().all() if z.point_ids else []
    settlement = settle.zone_context(db, z, overview) if overview else None
    return {"id": z.id, "zone_code": z.zone_code, "batch_id": z.batch_id, "cluster_label": z.cluster_label, "name": z.name, "status": z.status, "point_ids": z.point_ids or [], "point_codes": [p.point_code for p in point_rows], "center_x": z.center_x, "center_y": z.center_y, "average_settlement_mm": z.average_settlement_mm, "max_settlement_mm": z.max_settlement_mm, "local_density": z.local_density, "boundary": z.boundary, "original_cluster_evidence": z.original_cluster_evidence, "split_from_id": z.split_from_id, "conflict_notice_id": z.conflict_notice_id, "split_reason": z.split_reason, "conflict_road": notice.road_name if notice else None, "settlement": settlement}

def notice_view(n):
    return {"id": n.id, "notice_code": n.notice_code, "zone_id": n.zone_id, "road_name": n.road_name, "contractor": n.contractor, "permit_no": n.permit_no, "starts_at": iso(n.starts_at), "ends_at": iso(n.ends_at), "boundary": n.boundary, "status": n.status, "lane_closure_required": n.lane_closure_required, "conflict_note": n.conflict_note}

def resurvey_view(r):
    return {"id": r.id, "record_code": r.record_code, "zone_id": r.zone_id, "measured_at": iso(r.measured_at), "settlement_before_mm": r.settlement_before_mm, "settlement_after_mm": r.settlement_after_mm, "delta_mm": r.delta_mm, "pass_fail": r.pass_fail, "status": r.status, "verified_by": r.verified_by, "notes": r.notes}

@app.post("/api/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {"access_token": create_token(user), "token_type": "bearer", "user": user_view(user)}

@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_view(user)

@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    batches = db.execute(select(SurveyBatch).order_by(SurveyBatch.measured_at.desc())).scalars().all()
    assets = db.execute(select(ManholeAsset).order_by(ManholeAsset.asset_code)).scalars().all()
    points = db.execute(select(SurveyPoint).order_by(SurveyPoint.id)).scalars().all()
    zones = db.execute(select(RepairZone).order_by(RepairZone.id)).scalars().all()
    notices = db.execute(select(WorkNotice).order_by(WorkNotice.id)).scalars().all()
    resurveys = db.execute(select(Resurvey).order_by(Resurvey.id.desc())).scalars().all()
    jobs = db.execute(select(AnalysisJob).order_by(AnalysisJob.id.desc())).scalars().all()
    current_batch = batches[0] if batches else None
    overview = settle.settlement_overview(db, current_batch) if current_batch else None
    return {"user": user_view(user), "stats": {"assets": len(assets), "points": len(points), "zones": len(zones), "open_zones": sum(z.status not in ["closed", "split"] for z in zones), "notices": len(notices), "resurveys": len(resurveys)}, "batches": [batch_view(b) for b in batches], "assets": [asset_view(a) for a in assets], "points": [point_view(p) for p in points], "zones": [zone_view(z, db, overview) for z in zones], "notices": [notice_view(n) for n in notices], "resurveys": [resurvey_view(r) for r in resurveys], "jobs": [{"id": j.id, "job_code": j.job_code, "batch_id": j.batch_id, "status": j.status, "result": j.result, "error": j.error, "finished_at": iso(j.finished_at)} for j in jobs], "settlement": overview}

@app.get("/api/survey-batches/{batch_id}")
def batch_detail(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = db.get(SurveyBatch, batch_id)
    if not batch:
        raise HTTPException(404, "测量批次不存在")
    view = batch_view(batch)
    view["points"] = [point_view(p) for p in batch.points]
    return view

@app.post("/api/survey-batches")
def create_batch(payload: BatchIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    if db.execute(select(SurveyBatch).where(SurveyBatch.batch_code == payload.batch_code)).scalar_one_or_none():
        raise HTTPException(409, "批次编号已存在")
    batch = SurveyBatch(batch_code=payload.batch_code, measured_at=payload.measured_at, instrument=payload.instrument, coordinate_reference=payload.coordinate_reference, notes=payload.notes, warning_limit_mm=payload.warning_limit_mm, default_tolerance_mm=payload.default_tolerance_mm, status="open", settlement_state="pending")
    db.add(batch); record(db, user.id, "create_batch", "SurveyBatch", payload.batch_code, {"coordinate_reference": payload.coordinate_reference, "warning_limit_mm": payload.warning_limit_mm}); db.commit(); db.refresh(batch)
    return {"id": batch.id, "batch_code": batch.batch_code, "status": batch.status, "warning_limit_mm": batch.warning_limit_mm, "default_tolerance_mm": batch.default_tolerance_mm}

@app.post("/api/survey-batches/{batch_id}/points")
def add_point(batch_id: int, payload: SurveyPointIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    batch = db.get(SurveyBatch, batch_id)
    asset = db.get(ManholeAsset, payload.manhole_id)
    if not batch or not asset:
        raise HTTPException(404, "批次或井盖资产不存在")
    if batch.status in ["clustered", "queued"]:
        raise HTTPException(409, "批次已进入聚类流程，不能追加测量点")
    if db.execute(select(SurveyPoint).where(SurveyPoint.point_code == payload.point_code)).scalar_one_or_none():
        raise HTTPException(409, "测量点编号已存在")
    point = SurveyPoint(batch_id=batch_id, **payload.model_dump())
    batch.settlement_dirty = True; batch.settlement_state = "dirty"
    db.add(point); record(db, user.id, "add_survey_point", "SurveyPoint", payload.point_code, {"batch_code": batch.batch_code}); db.commit(); db.refresh(point)
    return point_view(point)

@app.post("/api/survey-points/{point_id}/correct-datum")
def correct_datum(point_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    point = db.get(SurveyPoint, point_id)
    if not point:
        raise HTTPException(404, "测量点不存在")
    batch = db.get(SurveyBatch, point.batch_id)
    old = point.coordinate_reference; point.coordinate_reference = batch.coordinate_reference; point.quality_status = "datum_corrected"
    if batch.status == "blocked": batch.status = "ready"
    record(db, user.id, "correct_coordinate_reference", "SurveyPoint", point.point_code, {"from": old, "to": point.coordinate_reference}); db.commit()
    return {"point_code": point.point_code, "coordinate_reference": point.coordinate_reference, "batch_status": batch.status}

def _require_batch(db, batch_id, lock=False):
    stmt = select(SurveyBatch).where(SurveyBatch.id == batch_id)
    if lock:
        stmt = stmt.with_for_update()
    batch = db.execute(stmt).scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "测量批次不存在")
    return batch

def _mark_dirty(batch):
    batch.settlement_dirty = True
    batch.settlement_state = "dirty"

@app.put("/api/survey-batches/{batch_id}/limits")
def update_batch_limits(batch_id: int, payload: BatchLimitsIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    batch = _require_batch(db, batch_id, lock=True)
    changes = {}
    if payload.warning_limit_mm is not None:
        batch.warning_limit_mm = payload.warning_limit_mm; changes["warning_limit_mm"] = payload.warning_limit_mm
    if payload.default_tolerance_mm is not None:
        batch.default_tolerance_mm = payload.default_tolerance_mm; changes["default_tolerance_mm"] = payload.default_tolerance_mm
    if not changes:
        raise HTTPException(422, "没有需要更新的阈值")
    _mark_dirty(batch)
    record(db, user.id, "update_settlement_limits", "SurveyBatch", batch.batch_code, changes)
    db.commit()
    return batch_view(batch)

@app.put("/api/survey-points/{point_id}/limits")
def update_point_limits(point_id: int, payload: PointLimitsIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    point = db.get(SurveyPoint, point_id)
    if not point:
        raise HTTPException(404, "测量点不存在")
    if payload.limit_lower_mm is not None and payload.limit_upper_mm is not None and payload.limit_lower_mm > payload.limit_upper_mm:
        raise HTTPException(422, "沉降下限不能大于上限")
    batch = _require_batch(db, point.batch_id, lock=True)
    point.limit_lower_mm = payload.limit_lower_mm
    point.limit_upper_mm = payload.limit_upper_mm
    point.tolerance_mm = payload.tolerance_mm
    _mark_dirty(batch)
    record(db, user.id, "update_point_settlement_limits", "SurveyPoint", point.point_code, payload.model_dump())
    db.commit()
    return point_view(point)

@app.put("/api/survey-points/{point_id}/reading")
def correct_reading(point_id: int, payload: ReadingCorrectionIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    """热区页面直接订正沉降读数；若已签发预警，重算后会自动比对范围。"""
    point = db.get(SurveyPoint, point_id)
    if not point:
        raise HTTPException(404, "测量点不存在")
    batch = _require_batch(db, point.batch_id, lock=True)
    before = point.settlement_mm
    point.settlement_mm = round(payload.settlement_mm, 3)
    if payload.elevation_mm is not None:
        point.elevation_mm = round(payload.elevation_mm, 3)
    point.quality_status = "reading_corrected"
    _mark_dirty(batch)
    record(db, user.id, "correct_settlement_reading", "SurveyPoint", point.point_code, {"from": before, "to": point.settlement_mm, "note": payload.note})
    db.commit()
    return {"point": point_view(point), "batch": batch_view(batch)}

@app.post("/api/survey-batches/{batch_id}/settlement/recompute")
def recompute_settlement(batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    batch = _require_batch(db, batch_id, lock=True)
    run, plates, correction = settle.create_run(db, batch, trigger="manual", actor_id=user.id)
    record(db, user.id, "recompute_settlement", "SettlementRun", f"{batch.batch_code}-R{run.revision}", {"summary": run.summary, "correction": correction.correction_code if correction else None})
    db.commit()
    db.refresh(run)
    return {"run": settle.run_view(run), "plates": {category: settle.plate_view(plates[category]) for category in settle.PLATE_CATEGORIES}, "correction": settle.correction_view(correction) if correction else None, "overview": settle.settlement_overview(db, batch)}

@app.get("/api/survey-batches/{batch_id}/settlement")
def settlement_status(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = _require_batch(db, batch_id)
    return settle.settlement_overview(db, batch)

@app.get("/api/survey-batches/{batch_id}/settlement/runs")
def settlement_runs(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_batch(db, batch_id)
    runs = db.execute(select(SettlementRun).where(SettlementRun.batch_id == batch_id).order_by(SettlementRun.revision.desc())).scalars().all()
    return [settle.run_view(run) for run in runs]

@app.get("/api/settlement-plates/{plate_id}/image.svg")
def settlement_plate_image(plate_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    plate = db.get(SettlementPlate, plate_id)
    if not plate:
        raise HTTPException(404, "成片不存在")
    from fastapi.responses import Response
    return Response(content=plate.image_svg or "", media_type="image/svg+xml", headers={"Content-Disposition": f'inline; filename="{plate.plate_code}.svg"'})

@app.post("/api/survey-batches/{batch_id}/warnings")
def issue_warning(batch_id: int, payload: WarningSignoffIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    """签发道路预警：只有必超限成片可签，可能超限成片留待复测。

    签发与订正并发：先订正（批次已标脏）则要求先重算；先签发则保留原单，
    重算后由 ``maybe_issue_correction`` 按范围变化发更正单。
    """
    batch = _require_batch(db, batch_id, lock=True)
    existing = db.execute(select(RoadWarning).where(RoadWarning.batch_id == batch_id)).scalar_one_or_none()
    if existing:
        raise HTTPException(409, {"code": "WARNING_ALREADY_ISSUED", "message": "该批次道路预警已签发；范围变化请走更正单", "warning_code": existing.warning_code, "status": existing.status})
    if batch.settlement_dirty:
        raise HTTPException(409, {"code": "SETTLEMENT_PENDING_RECOMPUTE", "message": "读数或上下限已订正，请先重算沉降范围再签发"})
    run = settle.latest_run(db, batch.id)
    if not run:
        run, plates, _ = settle.create_run(db, batch, trigger="signoff", actor_id=user.id)
    else:
        plates = settle.run_plates(db, run)
    plate = plates.get(settle.CATEGORY_CERTAIN)
    if not plate or not plate.point_codes:
        raise HTTPException(409, {"code": "NO_CERTAIN_PLATE", "message": "没有必超限成片，不能签发道路预警；可能超限成片请转复测"})
    road_name = (payload.road_name or "").strip() or "、".join(plate.roads) or batch.batch_code
    warning = RoadWarning(
        warning_code=f"RW-{batch.batch_code}-R{run.revision}",
        batch_id=batch.id,
        plate_id=plate.id,
        run_id=run.id,
        revision=run.revision,
        road_name=road_name,
        status="signed",
        scope=settle._certain_scope(plate),
        basis={"run_revision": run.revision, "plate_code": plate.plate_code, "scale_px_per_m": plate.scale_px_per_m, "assessment": [row for row in run.assessments if row["category"] == settle.CATEGORY_CERTAIN], "note": payload.note},
        signed_by=user.id,
        signed_at=datetime.utcnow(),
    )
    plate.status = "warning_issued"
    db.add(warning)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {"code": "WARNING_ALREADY_ISSUED", "message": "该批次道路预警已签发，请刷新"})
    record(db, user.id, "issue_road_warning", "RoadWarning", warning.warning_code, {"run_revision": run.revision, "point_codes": plate.point_codes, "roads": plate.roads})
    db.commit()
    db.refresh(warning)
    return {"warning": settle.warning_view(warning), "plate": settle.plate_view(plate), "overview": settle.settlement_overview(db, batch)}

@app.post("/api/road-warnings/{warning_id}/corrections")
def issue_correction(warning_id: int, payload: CorrectionIssueIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    """补发更正单；同一重算版本或同一幂等键重试不重复签发。"""
    warning = db.get(RoadWarning, warning_id)
    if not warning:
        raise HTTPException(404, "道路预警不存在")
    if warning.status not in ("signed",):
        raise HTTPException(409, "只有已签发的道路预警才能发更正单")
    batch = db.get(SurveyBatch, warning.batch_id)
    if payload.run_id is not None:
        run = db.get(SettlementRun, payload.run_id)
        if not run or run.batch_id != batch.id:
            raise HTTPException(404, "重算版本不存在")
        plates = settle.run_plates(db, run)
    else:
        run = settle.latest_run(db, batch.id)
        plates = settle.run_plates(db, run)
    plate = plates.get(settle.CATEGORY_CERTAIN)
    if not plate:
        raise HTTPException(409, "该重算版本没有必超限成片")
    correction, created = settle.issue_correction_retry_safe(db, warning, run, plate, payload.idempotency_key, payload.reason, user.id)
    view = settle.correction_view(correction)
    if created:
        record(db, user.id, "issue_warning_correction", "WarningCorrection", correction.correction_code, {"warning": warning.warning_code, "change_type": correction.change_type, "added": correction.added_point_codes, "removed": correction.removed_point_codes})
        db.commit(); db.refresh(correction)
        view = settle.correction_view(correction)
    else:
        db.rollback()
    return {"created": created, "correction": view, "overview": settle.settlement_overview(db, batch)}

@app.post("/api/warning-corrections/{correction_id}/acknowledge")
def acknowledge_correction(correction_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    correction = db.get(WarningCorrection, correction_id)
    if not correction:
        raise HTTPException(404, "更正单不存在")
    if correction.status != "issued":
        raise HTTPException(409, "更正单不在待接收状态")
    correction.status = "acknowledged"; correction.signed_by = user.id; correction.signed_at = datetime.utcnow()
    record(db, user.id, "acknowledge_warning_correction", "WarningCorrection", correction.correction_code, {})
    db.commit()
    return settle.correction_view(correction)

@app.get("/api/road-warnings")
def road_warnings(batch_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(RoadWarning).order_by(RoadWarning.id.desc())
    if batch_id is not None:
        stmt = stmt.where(RoadWarning.batch_id == batch_id)
    return [settle.warning_view(w) for w in db.execute(stmt).scalars().all()]

@app.post("/api/survey-batches/{batch_id}/cluster")
def enqueue_cluster(batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    batch = db.get(SurveyBatch, batch_id)
    if not batch:
        raise HTTPException(404, "测量批次不存在")
    points = build_input(db, batch)
    refs = {batch.coordinate_reference, *(p["coordinate_reference"] for p in points)}
    if len(refs) != 1:
        batch.status = "blocked"
        record(db, user.id, "block_cluster_datum_conflict", "SurveyBatch", batch.batch_code, {"references": sorted(refs)})
        db.commit()
        conflicts = [p["point_code"] for p in points if p["coordinate_reference"] != batch.coordinate_reference]
        raise HTTPException(status_code=409, detail={"code": "DATUM_CONFLICT", "message": "测量点存在坐标基准冲突，禁止聚类", "point_codes": conflicts, "batch_status": batch.status})
    job = AnalysisJob(job_code=f"JOB-{batch.batch_code}-{int(datetime.now().timestamp())}", batch_id=batch.id, status="queued", input_summary={"points": points, "params": {"eps_m": 26.0, "min_samples": 3}})
    batch.status = "queued"; db.add(job); record(db, user.id, "enqueue_spatial_cluster", "AnalysisJob", job.job_code, {"batch_code": batch.batch_code, "point_count": len(points)}); db.commit(); db.refresh(job)
    redis_client.rpush("manhole_settlement_jobs", str(job.id))
    return {"job_id": job.id, "job_code": job.job_code, "status": job.status}

@app.get("/api/analysis-jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = db.get(AnalysisJob, job_id)
    if not job: raise HTTPException(404, "分析任务不存在")
    return {"id": job.id, "job_code": job.job_code, "status": job.status, "result": job.result, "error": job.error, "finished_at": iso(job.finished_at)}

@app.get("/api/zones")
def zones(db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = db.execute(select(SurveyBatch).order_by(SurveyBatch.measured_at.desc())).scalars().first()
    overview = settle.settlement_overview(db, batch) if batch else None
    return [zone_view(z, db, overview) for z in db.execute(select(RepairZone).order_by(RepairZone.id)).scalars().all()]

@app.post("/api/zones/{zone_id}/split")
def split_zone(zone_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    if zone.status != "needs_split": raise HTTPException(409, "当前片区没有待拆分的道路边界冲突")
    points = db.execute(select(SurveyPoint).where(SurveyPoint.id.in_(zone.point_ids))).scalars().all()
    notice = db.get(WorkNotice, zone.conflict_notice_id)
    ordered = sorted(points, key=lambda p: (p.x, p.y))
    split_x = (notice.boundary["min_x"] + notice.boundary["max_x"]) / 2 if notice else ordered[len(ordered)//2].x
    left = [p for p in ordered if p.x <= split_x]
    right = [p for p in ordered if p.x > split_x]
    if not left or not right:
        half = max(1, len(ordered)//2); left, right = ordered[:half], ordered[half:]
    zone.status = "split"; zone.split_reason = f"依据 {notice.road_name if notice else '道路边界'} 在 x={round(split_x, 2)} 拆分，保留原始聚类证据"
    children = []
    for idx, members in enumerate([left, right], 1):
        b = bounds([{ "x": p.x, "y": p.y } for p in members])
        child = RepairZone(zone_code=f"{zone.zone_code}-S{idx}", batch_id=zone.batch_id, cluster_label=zone.cluster_label, name=f"{zone.name} / 边界片 {idx}", status="ready_for_notice", point_ids=[p.id for p in members], center_x=round(sum(p.x for p in members)/len(members),2), center_y=round(sum(p.y for p in members)/len(members),2), average_settlement_mm=round(sum(p.settlement_mm for p in members)/len(members),2), max_settlement_mm=max(p.settlement_mm for p in members), local_density=zone.local_density, boundary=b, original_cluster_evidence={"split_from": zone.zone_code, "split_axis": "x", "split_x": split_x, "source_evidence": zone.original_cluster_evidence}, split_from_id=zone.id)
        db.add(child); children.append(child)
    record(db, user.id, "split_zone_by_road_boundary", "RepairZone", zone.zone_code, {"children": [f"{zone.zone_code}-S1", f"{zone.zone_code}-S2"], "split_x": split_x})
    db.commit()
    return {"source_zone": zone.zone_code, "children": [c.zone_code for c in children], "status": zone.status, "split_reason": zone.split_reason}

@app.post("/api/zones/{zone_id}/work-notices")
def create_notice(zone_id: int, payload: NoticeIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    if zone.status not in ["proposed", "ready_for_notice"]: raise HTTPException(409, "当前片区不能发起施工联动")
    notice = WorkNotice(notice_code=f"WN-{zone.zone_code}-{int(datetime.now().timestamp())}", zone_id=zone.id, road_name=payload.road_name, contractor=payload.contractor, permit_no=payload.permit_no, starts_at=payload.starts_at, ends_at=payload.ends_at, boundary=zone.boundary, status="submitted", lane_closure_required=payload.lane_closure_required, created_by=user.id)
    db.add(notice); zone.status = "notice_sent"; record(db, user.id, "submit_work_notice", "WorkNotice", notice.notice_code, {"zone_code": zone.zone_code}); db.commit(); db.refresh(notice)
    return notice_view(notice)

@app.post("/api/work-notices/{notice_id}/acknowledge")
def acknowledge_notice(notice_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    notice = db.get(WorkNotice, notice_id)
    if not notice: raise HTTPException(404, "施工联动单不存在")
    if notice.status != "submitted": raise HTTPException(409, "施工联动单不在待确认状态")
    notice.status = "active"; record(db, user.id, "acknowledge_work_notice", "WorkNotice", notice.notice_code, {"status": "active"}); db.commit()
    return notice_view(notice)

@app.post("/api/zones/{zone_id}/start-repair")
def start_repair(zone_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    notice = db.execute(select(WorkNotice).where(WorkNotice.zone_id == zone.id, WorkNotice.status == "active")).scalar_one_or_none()
    if not notice: raise HTTPException(409, "施工联动单未激活，不能开始维修")
    zone.status = "repairing"; record(db, user.id, "start_repair", "RepairZone", zone.zone_code, {"notice_code": notice.notice_code}); db.commit()
    return zone_view(zone, db)

@app.post("/api/zones/{zone_id}/resurvey")
def submit_resurvey(zone_id: int, payload: ResurveyIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    if zone.status != "repairing": raise HTTPException(409, "片区未进入维修中，不能提交复测")
    after = round(payload.settlement_after_mm, 2); before = zone.average_settlement_mm; delta = round(before - after, 2); pass_fail = "pass" if after <= 8.0 else "fail"
    record_code = f"RS-{zone.zone_code}-{int(datetime.now().timestamp())}"
    resurvey = Resurvey(record_code=record_code, zone_id=zone.id, measured_at=payload.measured_at, settlement_before_mm=before, settlement_after_mm=after, delta_mm=delta, pass_fail=pass_fail, status="submitted", notes=payload.notes)
    db.add(resurvey); zone.status = "awaiting_resurvey"; record(db, user.id, "submit_resurvey", "Resurvey", record_code, {"zone_code": zone.zone_code, "pass_fail": pass_fail}); db.commit(); db.refresh(resurvey)
    return resurvey_view(resurvey)

@app.post("/api/resurveys/{resurvey_id}/verify")
def verify_resurvey(resurvey_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    resurvey = db.get(Resurvey, resurvey_id)
    if not resurvey: raise HTTPException(404, "复测记录不存在")
    if resurvey.status != "submitted": raise HTTPException(409, "复测记录不在待核验状态")
    zone = db.get(RepairZone, resurvey.zone_id)
    resurvey.verified_by = user.id
    if resurvey.pass_fail == "pass":
        resurvey.status = "verified"; zone.status = "closed"
        notice = db.execute(select(WorkNotice).where(WorkNotice.zone_id == zone.id)).scalar_one_or_none()
        if notice: notice.status = "completed"
    else:
        resurvey.status = "rejected"; zone.status = "repairing"
    record(db, user.id, "verify_resurvey", "Resurvey", resurvey.record_code, {"result": resurvey.pass_fail, "zone_status": zone.status}); db.commit()
    return {"resurvey": resurvey_view(resurvey), "zone": zone_view(zone, db)}

@app.get("/api/audit-logs")
def audit_logs(db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor", "viewer"))):
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(80)).scalars().all()
    return [{"id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "detail": r.detail, "created_at": iso(r.created_at)} for r in rows]
