"""Jinja2 page rendering routes."""

from pathlib import Path

from loguru import logger

from fastapi import APIRouter, Depends, Request, Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_web_user
from app.models.database import get_db
from app.models.fa_case import FACase, FAReport, FAReportSlide, FAUser, FAWeeklyPeriod

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(
    directory=Path(__file__).resolve().parent.parent / "templates"
)


def _user_ctx(request: Request, user: FAUser) -> dict:
    """Build common template context from authenticated user."""
    return {
        "request": request,
        "user": user,
        "scopes": getattr(user, "jwt_scopes", []),
    }


@router.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    return templates.TemplateResponse("home.html", _user_ctx(request, user))


@router.get("/weeks", response_class=HTMLResponse)
async def weeks_list_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    weeks: list = []
    try:
        result = await db.execute(
            select(
                FAWeeklyPeriod,
                func.count(func.distinct(FAReport.id)).label("report_count"),
                func.count(FACase.id).label("case_count"),
            )
            .outerjoin(FAReport)
            .outerjoin(FACase)
            .group_by(FAWeeklyPeriod.id)
            .order_by(FAWeeklyPeriod.year.desc(), FAWeeklyPeriod.week_number.desc())
            .limit(20)
        )
        weeks = [
            {
                "period": row[0],
                "report_count": row[1],
                "case_count": row[2],
            }
            for row in result.all()
        ]
    except Exception:
        logger.warning("DB unavailable, showing empty data")

    return templates.TemplateResponse(
        "weeks_list.html",
        {**_user_ctx(request, user), "weeks": weeks},
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    user: FAUser = Security(get_web_user, scopes=["write"]),
):
    return templates.TemplateResponse("upload.html", _user_ctx(request, user))


@router.get("/reports/{report_id}/triage", response_class=HTMLResponse)
async def triage_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        return RedirectResponse(url="/")

    slides_result = await db.execute(
        select(FAReportSlide)
        .where(FAReportSlide.report_id == report_id)
        .order_by(FAReportSlide.slide_number)
    )
    slides = slides_result.scalars().all()

    slides_data = [
        {
            "id": s.id,
            "slide_number": s.slide_number,
            "image_path": s.image_path,
            "is_candidate": s.is_candidate,
            "classification_status": s.classification_status,
            "classification_confidence": s.classification_confidence,
            "is_case_page": s.is_case_page,
        }
        for s in slides
    ]

    return templates.TemplateResponse(
        "triage.html",
        {**_user_ctx(request, user), "report": report, "slides_json": slides_data},
    )


@router.get("/reports/{report_id}/review", response_class=HTMLResponse)
async def review_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        return RedirectResponse(url="/")

    return templates.TemplateResponse(
        "review.html",
        {**_user_ctx(request, user), "report": report},
    )


@router.get("/reports/{report_id}/slides", response_class=HTMLResponse)
async def report_slides_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        return RedirectResponse(url="/")

    slides_result = await db.execute(
        select(FAReportSlide)
        .where(FAReportSlide.report_id == report_id)
        .order_by(FAReportSlide.slide_number)
    )
    slides = slides_result.scalars().all()

    return templates.TemplateResponse(
        "report_slides.html",
        {**_user_ctx(request, user), "report": report, "slides": slides},
    )


@router.get("/cases", response_class=HTMLResponse)
async def cases_page(
    request: Request,
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    return templates.TemplateResponse("case_list.html", _user_ctx(request, user))


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail_page(
    case_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    result = await db.execute(select(FACase).where(FACase.id == case_id))
    case = result.scalar_one_or_none()
    if not case:
        return RedirectResponse(url="/cases")

    return templates.TemplateResponse(
        "case_detail.html",
        {**_user_ctx(request, user), "case": case},
    )


@router.get("/weeks/{period_id}", response_class=HTMLResponse)
async def week_detail_page(
    period_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    result = await db.execute(
        select(FAWeeklyPeriod).where(FAWeeklyPeriod.id == period_id)
    )
    period = result.scalar_one_or_none()
    if not period:
        return RedirectResponse(url="/")

    reports_result = await db.execute(
        select(FAReport, FAUser.employee_name)
        .join(FAUser, FAReport.uploader_id == FAUser.id)
        .where(FAReport.weekly_period_id == period_id)
        .order_by(FAReport.created_at.desc())
    )
    reports = [
        {"report": row[0], "uploader_name": row[1]} for row in reports_result.all()
    ]

    return templates.TemplateResponse(
        "week_detail.html",
        {**_user_ctx(request, user), "period": period, "reports": reports},
    )
