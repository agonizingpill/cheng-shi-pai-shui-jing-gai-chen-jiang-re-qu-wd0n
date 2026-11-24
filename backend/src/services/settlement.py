"""Settlement limit assessment, original-scale plates and warning/correction rules.

口径（与前端、核验视图一致）：

* 每个测点可单独录入沉降下限 ``limit_lower_mm``、上限 ``limit_upper_mm``
  与测量误差 ``tolerance_mm``；未录入时上限回落到批次警戒线，误差回落到批次默认误差。
* ``settlement > 上限`` 为超限；``settlement < 下限``（若录入）为低于下限。
* 只有"算上误差仍超限"才是**必超限**：上限方向的 ``reading - tolerance > 上限``，
  下限方向的 ``reading + tolerance < 下限``。读数刚过线、减去误差后回到线内的，
  属于**可能超限**，成片后等待复测。
* 必超限与可能超限各出一张**原尺度**成片：同一批次、同一个像素/米比例与坐标原点，
  两张片子可以叠合比对，不做拉伸适配。
* 只有必超限成片才能签发道路预警；可能超限成片保持待复测。
"""

import html
from datetime import datetime

from ..models import (
    RepairZone,
    RoadWarning,
    SettlementPlate,
    SettlementRun,
    SurveyBatch,
    SurveyPoint,
    WarningCorrection,
)

DEFAULT_WARNING_LIMIT_MM = 12.0
DEFAULT_TOLERANCE_MM = 0.5
CANVAS_WIDTH = 780
CANVAS_HEIGHT = 560
CANVAS_MARGIN = 58
MIN_SPAN_M = 24.0
CATEGORY_CERTAIN = "certain"
CATEGORY_POSSIBLE = "possible"
CATEGORY_WITHIN = "within"
CATEGORY_EXCLUDED = "excluded"
PLATE_CATEGORIES = (CATEGORY_CERTAIN, CATEGORY_POSSIBLE)

_EPS = 1e-9


def _round(value, digits=3):
    return None if value is None else round(float(value), digits)


def effective_limits(point, batch):
    upper = point.limit_upper_mm if point.limit_upper_mm is not None else batch.warning_limit_mm
    lower = point.limit_lower_mm
    tolerance = point.tolerance_mm if point.tolerance_mm is not None else batch.default_tolerance_mm
    upper = DEFAULT_WARNING_LIMIT_MM if upper is None else float(upper)
    tolerance = DEFAULT_TOLERANCE_MM if tolerance is None else float(tolerance)
    if tolerance < 0:
        tolerance = 0.0
    return lower, upper, tolerance


def classify(point, batch):
    """返回单个测点的超限判定，误差带与判定类别都写清楚。"""
    lower, upper, tolerance = effective_limits(point, batch)
    reading = float(point.settlement_mm)
    datum_mismatch = point.coordinate_reference != batch.coordinate_reference
    base = {
        "point_id": point.id,
        "point_code": point.point_code,
        "asset_code": point.manhole.asset_code if point.manhole else "",
        "street_name": point.manhole.street_name if point.manhole else "",
        "x": point.x,
        "y": point.y,
        "settlement_mm": _round(reading, 3),
        "limit_lower_mm": _round(lower, 3),
        "limit_upper_mm": _round(upper, 3),
        "tolerance_mm": _round(tolerance, 3),
        "confidence_low_mm": _round(reading - tolerance, 3),
        "confidence_high_mm": _round(reading + tolerance, 3),
        "direction": None,
        "category": CATEGORY_WITHIN,
        "margin_mm": None,
        "limit_source": "point" if (point.limit_upper_mm is not None or point.limit_lower_mm is not None) else "batch",
        "excluded_reason": None,
    }
    if datum_mismatch:
        base["category"] = CATEGORY_EXCLUDED
        base["excluded_reason"] = f"坐标基准 {point.coordinate_reference} 与批次 {batch.coordinate_reference} 不一致，原尺度成片不可定位"
        return base

    confidence_low = float(base["confidence_low_mm"])
    confidence_high = float(base["confidence_high_mm"])
    if reading > upper + _EPS:
        base["direction"] = "over"
        base["margin_mm"] = _round(reading - upper, 3)
        base["category"] = CATEGORY_CERTAIN if confidence_low > upper + _EPS else CATEGORY_POSSIBLE
    elif lower is not None and reading < lower - _EPS:
        base["direction"] = "under"
        base["margin_mm"] = _round(reading - lower, 3)
        base["category"] = CATEGORY_CERTAIN if confidence_high < lower - _EPS else CATEGORY_POSSIBLE
    return base


def assess_batch(batch, points):
    rows = [classify(point, batch) for point in points]
    summary = {
        "certain": sum(1 for row in rows if row["category"] == CATEGORY_CERTAIN),
        "possible": sum(1 for row in rows if row["category"] == CATEGORY_POSSIBLE),
        "within": sum(1 for row in rows if row["category"] == CATEGORY_WITHIN),
        "excluded": sum(1 for row in rows if row["category"] == CATEGORY_EXCLUDED),
        "over": sum(1 for row in rows if row["direction"] == "over"),
        "under": sum(1 for row in rows if row["direction"] == "under"),
        "total": len(rows),
    }
    return rows, summary


def batch_points(db, batch):
    return (
        db.query(SurveyPoint)
        .filter(SurveyPoint.batch_id == batch.id)
        .order_by(SurveyPoint.id)
        .all()
    )


def latest_run(db, batch_id):
    return (
        db.query(SettlementRun)
        .filter(SettlementRun.batch_id == batch_id)
        .order_by(SettlementRun.revision.desc())
        .first()
    )


def run_plates(db, run):
    if not run:
        return {}
    rows = db.query(SettlementPlate).filter(SettlementPlate.run_id == run.id).all()
    return {plate.category: plate for plate in rows}


def warning_for_batch(db, batch_id):
    return db.query(RoadWarning).filter(RoadWarning.batch_id == batch_id).first()


def corrections_for_warning(db, warning_id):
    if not warning_id:
        return []
    return (
        db.query(WarningCorrection)
        .filter(WarningCorrection.warning_id == warning_id)
        .order_by(WarningCorrection.new_revision, WarningCorrection.id)
        .all()
    )


def canvas_layout(points):
    """整批共用的原尺度画布：同一比例、同一原点，两张片子可直接叠合。"""
    xs = [float(p.x) for p in points] or [0.0]
    ys = [float(p.y) for p in points] or [0.0]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, MIN_SPAN_M)
    span_y = max(max_y - min_y, MIN_SPAN_M)
    inner_w = CANVAS_WIDTH - 2 * CANVAS_MARGIN
    inner_h = CANVAS_HEIGHT - 2 * CANVAS_MARGIN
    scale = min(inner_w / span_x, inner_h / span_y)
    scale = round(max(scale, 0.5), 4)
    used_w = span_x * scale
    used_h = span_y * scale
    return {
        "width": CANVAS_WIDTH,
        "height": CANVAS_HEIGHT,
        "margin": CANVAS_MARGIN,
        "scale_px_per_m": scale,
        "origin_x": round(min_x, 3),
        "origin_y": round(max_y, 3),
        "world_min_x": round(min_x, 3),
        "world_max_x": round(max_x, 3),
        "world_min_y": round(min_y, 3),
        "world_max_y": round(max_y, 3),
        "offset_x": round((CANVAS_WIDTH - used_w) / 2, 3),
        "offset_y": round((CANVAS_HEIGHT - used_h) / 2, 3),
        "span_x_m": round(span_x, 3),
        "span_y_m": round(span_y, 3),
    }


def project(canvas, x, y):
    return (
        canvas["offset_x"] + (float(x) - canvas["origin_x"]) * canvas["scale_px_per_m"],
        canvas["offset_y"] + (canvas["origin_y"] - float(y)) * canvas["scale_px_per_m"],
    )


def plate_bounds(rows):
    if not rows:
        return {}
    xs = [row["x"] for row in rows]
    ys = [row["y"] for row in rows]
    return {
        "min_x": round(min(xs), 3),
        "max_x": round(max(xs), 3),
        "min_y": round(min(ys), 3),
        "max_y": round(max(ys), 3),
    }


def _limit_label(row):
    if row["direction"] == "under":
        return f"下限 {row['limit_lower_mm']} mm"
    return f"上限 {row['limit_upper_mm']} mm"


def render_plate_svg(batch, run, category, rows, canvas):
    title = "必超限成片" if category == CATEGORY_CERTAIN else "可能超限成片"
    accent = "#8d3f2a" if category == CATEGORY_CERTAIN else "#b5761f"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas["width"]}" height="{canvas["height"]}" '
        f'viewBox="0 0 {canvas["width"]} {canvas["height"]}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#f7f4ec"/>',
        f'<rect x="10" y="10" width="{canvas["width"] - 20}" height="{canvas["height"] - 20}" '
        f'fill="none" stroke="#c9bda6" stroke-width="1.5"/>',
        f'<text x="24" y="34" font-family="monospace" font-size="14" font-weight="700" fill="#2c241c">'
        f'{html.escape(title)} · {html.escape(batch.batch_code)} · R{run.revision}</text>',
        f'<text x="24" y="52" font-family="monospace" font-size="10" fill="#6b5f4d">'
        f'原尺度 {canvas["scale_px_per_m"]} px/m · 警戒线 {batch.warning_limit_mm} mm · '
        f'扩线容差 {batch.default_tolerance_mm} mm</text>',
    ]

    # 10 米坐标网格，保持两片同一参考系
    step = 10.0
    grid_start_x = int(canvas["world_min_x"] // step) * step
    for gx in _frange(grid_start_x, canvas["world_max_x"] + step, step):
        px, _ = project(canvas, gx, canvas["origin_y"])
        if canvas["margin"] * 0.35 <= px <= canvas["width"] - canvas["margin"] * 0.35:
            parts.append(
                f'<line x1="{px:.1f}" y1="70" x2="{px:.1f}" y2="{canvas["height"] - 42}" '
                f'stroke="#e2d8c4" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{px:.1f}" y="{canvas["height"] - 30}" font-family="monospace" '
                f'font-size="8" fill="#8c7f6a" text-anchor="middle">{int(gx)}</text>'
            )
    grid_start_y = int(canvas["world_min_y"] // step) * step
    for gy in _frange(grid_start_y, canvas["world_max_y"] + step, step):
        _, py = project(canvas, canvas["origin_x"], gy)
        if 70 <= py <= canvas["height"] - 42:
            parts.append(
                f'<line x1="24" y1="{py:.1f}" x2="{canvas["width"] - 24}" y2="{py:.1f}" '
                f'stroke="#e2d8c4" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="26" y="{py - 3:.1f}" font-family="monospace" font-size="8" '
                f'fill="#8c7f6a">{int(gy)}</text>'
            )

    # 按道路分组标注
    roads = {}
    for row in rows:
        roads.setdefault(row["street_name"] or "未标注道路", []).append(row)
    for road, members in roads.items():
        xs = [project(canvas, m["x"], m["y"])[0] for m in members]
        ys = [project(canvas, m["x"], m["y"])[1] for m in members]
        label_y = min(ys) - 13 if min(ys) - 13 > 68 else min(ys) + 22
        parts.append(
            f'<text x="{sum(xs) / len(xs):.1f}" y="{label_y:.1f}" font-family="monospace" '
            f'font-size="11" font-weight="600" fill="#4b3f30" text-anchor="middle">'
            f'{html.escape(road)}</text>'
        )

    for row in rows:
        px, py = project(canvas, row["x"], row["y"])
        radius = 7.5
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{radius}" fill="{accent}" '
            f'fill-opacity="0.22" stroke="{accent}" stroke-width="2"/>'
        )
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.2" fill="{accent}"/>'
        )
        direction = "低于下限" if row["direction"] == "under" else "超上限"
        parts.append(
            f'<text x="{px + 11:.1f}" y="{py - 5:.1f}" font-family="monospace" font-size="9" '
            f'fill="#2c241c">{html.escape(row["point_code"])}</text>'
        )
        parts.append(
            f'<text x="{px + 11:.1f}" y="{py + 6:.1f}" font-family="monospace" font-size="8" '
            f'fill="{accent}">{html.escape(row["asset_code"])} · {row["settlement_mm"]} mm · '
            f'{html.escape(direction)}</text>'
        )

    if not rows:
        parts.append(
            f'<text x="{canvas["width"] / 2}" y="{canvas["height"] / 2}" font-family="monospace" '
            f'font-size="13" fill="#8c7f6a" text-anchor="middle">本类别暂无测点</text>'
        )

    # 比例尺
    bar_m = _nice_scale_bar(canvas)
    bar_px = bar_m * canvas["scale_px_per_m"]
    bar_x = canvas["width"] - canvas["margin"] - bar_px
    bar_y = canvas["height"] - 28
    parts.append(
        f'<line x1="{bar_x:.1f}" y1="{bar_y}" x2="{bar_x + bar_px:.1f}" y2="{bar_y}" '
        f'stroke="#4b3f30" stroke-width="2"/>'
    )
    parts.append(
        f'<text x="{bar_x + bar_px / 2:.1f}" y="{bar_y - 5}" font-family="monospace" '
        f'font-size="9" fill="#4b3f30" text-anchor="middle">{bar_m:g} m</text>'
    )
    parts.append(
        f'<text x="24" y="{canvas["height"] - 16}" font-family="monospace" font-size="9" '
        f'fill="#6b5f4d">测点 {len(rows)} 个 · '
        f'{html.escape(title)}依据：读数与测点上下限、测量误差比较</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _frange(start, stop, step):
    value = start
    while value < stop:
        yield value
        value += step


def _nice_scale_bar(canvas):
    target_px = 120
    raw_m = target_px / max(canvas["scale_px_per_m"], 0.001)
    for candidate in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        if candidate >= raw_m:
            return candidate
    return 500


def create_run(db, batch, trigger, actor_id=None):
    """在当前读数上重算一版，写入 run 与两张原尺度成片。调用方负责加锁与提交。"""
    points = batch_points(db, batch)
    assessments, summary = assess_batch(batch, points)
    previous = latest_run(db, batch.id)
    revision = (previous.revision + 1) if previous else 1
    canvas = canvas_layout(points)

    run = SettlementRun(
        batch_id=batch.id,
        revision=revision,
        trigger=trigger,
        status="computed",
        warning_limit_mm=float(batch.warning_limit_mm),
        default_tolerance_mm=float(batch.default_tolerance_mm),
        scale_px_per_m=canvas["scale_px_per_m"],
        assessments=assessments,
        summary=summary,
        computed_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()

    plates = {}
    for category in PLATE_CATEGORIES:
        rows = [row for row in assessments if row["category"] == category]
        plate = SettlementPlate(
            plate_code=f"PL-{batch.batch_code}-R{revision}-{category.upper()}",
            run_id=run.id,
            batch_id=batch.id,
            revision=revision,
            category=category,
            roads=sorted({(row["street_name"] or "未标注道路") for row in rows}),
            point_ids=[row["point_id"] for row in rows],
            point_codes=[row["point_code"] for row in rows],
            point_count=len(rows),
            max_settlement_mm=max((row["settlement_mm"] for row in rows), default=None),
            min_settlement_mm=min((row["settlement_mm"] for row in rows), default=None),
            bounds=plate_bounds(rows),
            scale_px_per_m=canvas["scale_px_per_m"],
            canvas=canvas,
            image_svg=render_plate_svg(batch, run, category, rows, canvas),
            status=(
                "empty"
                if not rows
                else "pending_warning"
                if category == CATEGORY_CERTAIN
                else "pending_resurvey"
            ),
        )
        db.add(plate)
        plates[category] = plate
    db.flush()

    batch.settlement_revision = revision
    batch.settlement_dirty = False
    batch.settlement_state = "computed"

    correction = maybe_issue_correction(db, batch, run, plates, actor_id=actor_id, trigger=trigger)
    return run, plates, correction


def _certain_scope(plate):
    return {
        "revision": plate.revision,
        "plate_code": plate.plate_code,
        "roads": list(plate.roads or []),
        "point_codes": list(plate.point_codes or []),
        "point_count": plate.point_count,
        "bounds": plate.bounds,
        "max_settlement_mm": plate.max_settlement_mm,
        "min_settlement_mm": plate.min_settlement_mm,
    }


def _point_basis(assessments, codes):
    wanted = set(codes)
    return [row for row in assessments if row["point_code"] in wanted]


def effective_scope(db, warning):
    """预警当前生效范围：若已发过更正单，以最近一张更正单的范围为准。"""
    latest = (
        db.query(WarningCorrection)
        .filter(WarningCorrection.warning_id == warning.id)
        .order_by(WarningCorrection.new_revision.desc(), WarningCorrection.id.desc())
        .first()
    )
    if latest:
        return latest.scope_after, latest.new_revision
    return warning.scope or {}, warning.revision


def _change_type(before_codes, after_codes):
    before, after = set(before_codes), set(after_codes)
    if not after:
        return "cleared"
    if after > before:
        return "expanded"
    if after < before:
        return "reduced"
    return "replaced"


def maybe_issue_correction(db, batch, run, plates, actor_id=None, trigger="recompute"):
    """签发后重算：范围有变化才下更正单；与当前生效范围比较，重试不重复。"""
    warning = warning_for_batch(db, batch.id)
    if not warning or warning.status not in ("signed", "issued", "acknowledged"):
        return None
    plate = plates.get(CATEGORY_CERTAIN)
    if plate is None:
        return None

    before, before_revision = effective_scope(db, warning)
    before_codes = list(before.get("point_codes") or [])
    after_codes = list(plate.point_codes or [])
    before_roads = list(before.get("roads") or [])
    after_roads = list(plate.roads or [])
    if set(before_codes) == set(after_codes) and set(before_roads) == set(after_roads):
        return None

    existing = (
        db.query(WarningCorrection)
        .filter(
            WarningCorrection.warning_id == warning.id,
            WarningCorrection.new_revision == run.revision,
        )
        .first()
    )
    if existing:
        return existing

    added = sorted(set(after_codes) - set(before_codes))
    removed = sorted(set(before_codes) - set(after_codes))
    change_type = _change_type(before_codes, after_codes)

    basis = {
        "trigger": trigger,
        "warning_code": warning.warning_code,
        "base_revision": before_revision,
        "new_revision": run.revision,
        "before": before,
        "after": _certain_scope(plate),
        "added_points": _point_basis(run.assessments, added),
        "removed_point_codes": removed,
        "method": "同一批次重新按测点上下限与误差判定必超限范围，与当前生效范围比较",
    }
    correction = WarningCorrection(
        correction_code=f"WC-{warning.warning_code}-R{run.revision}",
        warning_id=warning.id,
        batch_id=batch.id,
        new_run_id=run.id,
        new_plate_id=plate.id,
        base_revision=before_revision,
        new_revision=run.revision,
        change_type=change_type,
        scope_before=before,
        scope_after=_certain_scope(plate),
        added_point_codes=added,
        removed_point_codes=removed,
        basis=basis,
        reason=f"重算后必超限范围由 {len(before_codes)} 点变为 {len(after_codes)} 点（{change_type}）",
        idempotency_key=f"auto:{warning.id}:{run.revision}",
        status="issued",
        issued_by=actor_id,
    )
    db.add(correction)
    db.flush()
    return correction


def issue_correction_retry_safe(db, warning, run, plate, idempotency_key, reason, actor_id):
    """显式补发/重试更正单：同一重算版本或同一幂等键只签发一次。"""
    if idempotency_key:
        existing = (
            db.query(WarningCorrection)
            .filter(WarningCorrection.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing, False
    existing = (
        db.query(WarningCorrection)
        .filter(
            WarningCorrection.warning_id == warning.id,
            WarningCorrection.new_revision == run.revision,
        )
        .first()
    )
    if existing:
        return existing, False

    after = _certain_scope(plate)
    before, before_revision = effective_scope(db, warning)
    added = sorted(set(after["point_codes"]) - set(before.get("point_codes") or []))
    removed = sorted(set(before.get("point_codes") or []) - set(after["point_codes"]))
    change_type = _change_type(before.get("point_codes") or [], after["point_codes"])
    basis = {
        "trigger": "manual",
        "warning_code": warning.warning_code,
        "base_revision": before_revision,
        "new_revision": run.revision,
        "before": before,
        "after": after,
        "added_points": _point_basis(run.assessments, added),
        "removed_point_codes": removed,
        "method": "按当前重算版本补发道路预警更正单",
    }
    correction = WarningCorrection(
        correction_code=f"WC-{warning.warning_code}-R{run.revision}",
        warning_id=warning.id,
        batch_id=warning.batch_id,
        new_run_id=run.id,
        new_plate_id=plate.id,
        base_revision=before_revision,
        new_revision=run.revision,
        change_type=change_type,
        scope_before=before,
        scope_after=after,
        added_point_codes=added,
        removed_point_codes=removed,
        basis=basis,
        reason=reason or f"重算后范围变化（{change_type}）",
        idempotency_key=idempotency_key or f"manual:{warning.id}:{run.revision}",
        status="issued",
        issued_by=actor_id,
    )
    db.add(correction)
    db.flush()
    return correction, True


def plate_view(plate):
    if not plate:
        return None
    return {
        "id": plate.id,
        "plate_code": plate.plate_code,
        "category": plate.category,
        "revision": plate.revision,
        "roads": plate.roads,
        "point_ids": plate.point_ids,
        "point_codes": plate.point_codes,
        "point_count": plate.point_count,
        "max_settlement_mm": plate.max_settlement_mm,
        "min_settlement_mm": plate.min_settlement_mm,
        "bounds": plate.bounds,
        "scale_px_per_m": plate.scale_px_per_m,
        "canvas": plate.canvas,
        "status": plate.status,
        "image_svg": plate.image_svg,
    }


def plate_light_view(plate):
    """列表用，不携带 SVG，避免 dashboard 过大。"""
    view = plate_view(plate)
    if view:
        view.pop("image_svg", None)
    return view



def warning_view(warning):
    if not warning:
        return None
    return {
        "id": warning.id,
        "warning_code": warning.warning_code,
        "batch_id": warning.batch_id,
        "plate_id": warning.plate_id,
        "run_id": warning.run_id,
        "revision": warning.revision,
        "road_name": warning.road_name,
        "status": warning.status,
        "scope": warning.scope,
        "basis": warning.basis,
        "signed_by": warning.signed_by,
        "signed_at": warning.signed_at.isoformat() if warning.signed_at else None,
        "created_at": warning.created_at.isoformat() if warning.created_at else None,
    }


def correction_view(correction):
    return {
        "id": correction.id,
        "correction_code": correction.correction_code,
        "warning_id": correction.warning_id,
        "batch_id": correction.batch_id,
        "new_run_id": correction.new_run_id,
        "new_plate_id": correction.new_plate_id,
        "base_revision": correction.base_revision,
        "new_revision": correction.new_revision,
        "change_type": correction.change_type,
        "scope_before": correction.scope_before,
        "scope_after": correction.scope_after,
        "added_point_codes": correction.added_point_codes,
        "removed_point_codes": correction.removed_point_codes,
        "basis": correction.basis,
        "reason": correction.reason,
        "status": correction.status,
        "issued_by": correction.issued_by,
        "issued_at": correction.issued_at.isoformat() if correction.issued_at else None,
        "signed_by": correction.signed_by,
        "signed_at": correction.signed_at.isoformat() if correction.signed_at else None,
    }


def run_view(run):
    if not run:
        return None
    return {
        "id": run.id,
        "revision": run.revision,
        "trigger": run.trigger,
        "status": run.status,
        "warning_limit_mm": run.warning_limit_mm,
        "default_tolerance_mm": run.default_tolerance_mm,
        "scale_px_per_m": run.scale_px_per_m,
        "summary": run.summary,
        "assessments": run.assessments,
        "computed_at": run.computed_at.isoformat() if run.computed_at else None,
    }


def settlement_overview(db, batch):
    run = latest_run(db, batch.id)
    warning = warning_for_batch(db, batch.id)
    plates = run_plates(db, run)
    corrections = corrections_for_warning(db, warning.id if warning else None)
    warning_data = warning_view(warning)
    if warning_data:
        scope, effective_revision = effective_scope(db, warning)
        warning_data["effective_scope"] = scope
        warning_data["effective_revision"] = effective_revision
    return {
        "batch_id": batch.id,
        "batch_code": batch.batch_code,
        "warning_limit_mm": batch.warning_limit_mm,
        "default_tolerance_mm": batch.default_tolerance_mm,
        "settlement_revision": batch.settlement_revision,
        "settlement_dirty": bool(batch.settlement_dirty),
        "settlement_state": batch.settlement_state,
        "latest_run": run_view(run),
        "plates": {category: plate_view(plates.get(category)) for category in PLATE_CATEGORIES},
        "warning": warning_data,
        "corrections": [correction_view(item) for item in corrections],
    }


def zone_context(db, zone, overview):
    """把预警/更正依据挂到热区卡片上，便于在热区页面直接追溯。"""
    certain, possible = set(), set()
    for row in (overview.get("latest_run") or {}).get("assessments") or []:
        if row.get("point_id") not in (zone.point_ids or []):
            continue
        if row.get("category") == CATEGORY_CERTAIN:
            certain.add(row["point_code"])
        elif row.get("category") == CATEGORY_POSSIBLE:
            possible.add(row["point_code"])
    warning = overview.get("warning")
    corrections = overview.get("corrections") or []
    return {
        "certain_point_codes": sorted(certain),
        "possible_point_codes": sorted(possible),
        "warning_code": warning["warning_code"] if warning else None,
        "warning_status": warning["status"] if warning else None,
        "correction_codes": [item["correction_code"] for item in corrections],
        "revision": overview.get("settlement_revision"),
        "dirty": overview.get("settlement_dirty"),
    }
