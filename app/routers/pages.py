"""Jinja2 page rendering routes."""

from pathlib import Path

from loguru import logger

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_payload, verify_token
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FACase, FAReport, FAUser, FAWeeklyPeriod

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

_DEV_USER = {"sub": "dev", "org_id": "dev"}


def _get_user_or_redirect(request: Request) -> dict | None:
    """Try to get current user from cookie. Returns None if not authenticated."""
    if settings.dev_skip_auth:
        return _DEV_USER
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        return verify_token(token)
    except Exception:
        return None


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    # Get weekly periods with counts
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

    return templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "weeks": weeks,
    })


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse("upload.html", {
        "request": request,
        "user": user,
    })


@router.get("/reports/{report_id}/review", response_class=HTMLResponse)
async def review_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    result = await db.execute(
        select(FAReport).where(FAReport.id == report_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        return RedirectResponse(url="/")

    return templates.TemplateResponse("review.html", {
        "request": request,
        "user": user,
        "report": report,
    })


@router.get("/cases", response_class=HTMLResponse)
async def cases_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse("case_list.html", {
        "request": request,
        "user": user,
    })


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail_page(
    case_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    result = await db.execute(
        select(FACase).where(FACase.id == case_id)
    )
    case = result.scalar_one_or_none()
    if not case:
        return RedirectResponse(url="/cases")

    return templates.TemplateResponse("case_detail.html", {
        "request": request,
        "user": user,
        "case": case,
    })


@router.get("/weeks/{period_id}", response_class=HTMLResponse)
async def week_detail_page(
    period_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    result = await db.execute(
        select(FAWeeklyPeriod).where(FAWeeklyPeriod.id == period_id)
    )
    period = result.scalar_one_or_none()
    if not period:
        return RedirectResponse(url="/")

    # Get reports for this week
    reports_result = await db.execute(
        select(FAReport, FAUser.employee_name)
        .join(FAUser, FAReport.uploader_id == FAUser.id)
        .where(FAReport.weekly_period_id == period_id)
        .order_by(FAReport.created_at.desc())
    )
    reports = [
        {"report": row[0], "uploader_name": row[1]}
        for row in reports_result.all()
    ]

    return templates.TemplateResponse("week_detail.html", {
        "request": request,
        "user": user,
        "period": period,
        "reports": reports,
    })
