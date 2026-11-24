from datetime import datetime
from pydantic import BaseModel, Field

class LoginIn(BaseModel):
    username: str
    password: str

class SurveyPointIn(BaseModel):
    point_code: str = Field(min_length=3, max_length=60)
    manhole_id: int
    x: float
    y: float
    elevation_mm: float
    settlement_mm: float = Field(ge=0, le=200)
    limit_lower_mm: float | None = Field(default=None, ge=0, le=200)
    limit_upper_mm: float | None = Field(default=None, ge=0, le=200)
    tolerance_mm: float | None = Field(default=None, ge=0, le=20)
    coordinate_reference: str
    measured_at: datetime

class BatchIn(BaseModel):
    batch_code: str
    measured_at: datetime
    instrument: str
    coordinate_reference: str
    notes: str = ""
    warning_limit_mm: float = Field(default=12.0, ge=0, le=200)
    default_tolerance_mm: float = Field(default=0.5, ge=0, le=20)

class BatchLimitsIn(BaseModel):
    warning_limit_mm: float | None = Field(default=None, ge=0, le=200)
    default_tolerance_mm: float | None = Field(default=None, ge=0, le=20)

class PointLimitsIn(BaseModel):
    limit_lower_mm: float | None = Field(default=None, ge=0, le=200)
    limit_upper_mm: float | None = Field(default=None, ge=0, le=200)
    tolerance_mm: float | None = Field(default=None, ge=0, le=20)

class ReadingCorrectionIn(BaseModel):
    settlement_mm: float = Field(ge=0, le=200)
    elevation_mm: float | None = None
    note: str = Field(default="", max_length=500)

class WarningSignoffIn(BaseModel):
    road_name: str | None = Field(default=None, max_length=200)
    note: str = Field(default="", max_length=500)

class CorrectionIssueIn(BaseModel):
    run_id: int | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=140)
    reason: str = Field(default="", max_length=500)

class NoticeIn(BaseModel):
    road_name: str
    contractor: str
    permit_no: str
    starts_at: datetime
    ends_at: datetime
    lane_closure_required: bool = False

class ResurveyIn(BaseModel):
    measured_at: datetime
    settlement_after_mm: float = Field(ge=0, le=200)
    notes: str = ""
