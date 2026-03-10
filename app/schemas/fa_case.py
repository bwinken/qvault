from datetime import date, datetime

from pydantic import BaseModel


# --- VLM response schema ---
class VLMExtractedData(BaseModel):
    date: str | None = None
    customer: str | None = None
    device: str | None = None
    model: str | None = None
    defect_mode: str | None = None
    defect_rate: str | None = None
    defect_lots: str | None = None
    fab_assembly: str | None = None
    fa_status: str | None = None
    follow_up: str | None = None


class VLMSlideResult(BaseModel):
    is_case_page: bool
    data: VLMExtractedData | None = None


# --- Slide processing result ---
class SlideExtractionResult(BaseModel):
    slide_number: int
    image_path: str
    is_case_page: bool
    skipped: bool = False  # True if pre-filter skipped this slide
    data: VLMExtractedData | None = None
    error: str | None = None


# --- Upload ---
class ReportUploadResponse(BaseModel):
    report_id: int
    filename: str
    total_slides: int
    slides: list[SlideExtractionResult]


# --- Case CRUD ---
class CaseEditRequest(BaseModel):
    date: str | None = None
    customer: str | None = None
    device: str | None = None
    model: str | None = None
    defect_mode: str | None = None
    defect_rate_raw: str | None = None
    defect_lots: list[str] | None = None
    fab_assembly: str | None = None
    fa_status: str | None = None
    follow_up: str | None = None


class CaseResponse(BaseModel):
    id: int
    report_id: int
    slide_number: int
    slide_image_path: str | None
    date: str | None
    customer: str | None
    device: str | None
    model: str | None
    defect_mode: str | None
    defect_rate_raw: str | None
    defect_lots: list[str] | None
    fab_assembly: str | None
    fa_status: str | None
    follow_up: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Report ---
class ReportResponse(BaseModel):
    id: int
    filename: str
    total_slides: int
    status: str
    uploader_name: str = ""
    created_at: datetime
    case_count: int = 0

    model_config = {"from_attributes": True}


# --- Weekly Period ---
class WeeklyPeriodResponse(BaseModel):
    id: int
    year: int
    week_number: int
    start_date: date
    end_date: date
    report_count: int = 0
    case_count: int = 0

    model_config = {"from_attributes": True}


class WeeklyPeriodCreate(BaseModel):
    year: int
    week_number: int


# --- Confirm save (review → DB) ---
class ConfirmSaveRequest(BaseModel):
    """Sent when user confirms reviewed cases for saving."""
    cases: list[CaseEditRequest]
    slide_numbers: list[int]
