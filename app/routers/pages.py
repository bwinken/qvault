"""Jinja2 page rendering routes."""

import json
from pathlib import Path

from loguru import logger

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import verify_token
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FACase, FAReport, FAReportSlide, FAUser, FAWeeklyPeriod

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(
    directory=Path(__file__).resolve().parent.parent / "templates"
)

_DEV_USER = {"sub": "dev", "org_id": "dev", "scopes": ["read", "write", "admin"]}


def _get_user_or_redirect(request: Request) -> dict | None:
    """Try to get current user from Authorization header (injected by Nginx/oauth2-proxy).

    Returns None if not authenticated. In production, Nginx auth_request
    handles the redirect to oauth2-proxy, so None should rarely occur.
    """
    if settings.dev_skip_auth:
        return _DEV_USER
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    try:
        return verify_token(auth_header[7:])
    except Exception:
        return None


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
        },
    )


@router.get("/weeks", response_class=HTMLResponse)
async def weeks_list_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

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
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
            "weeks": weeks,
        },
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")
    if "write" not in user.get("scopes", []):
        return RedirectResponse(url="/")

    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
        },
    )


@router.get("/reports/{report_id}/triage", response_class=HTMLResponse)
async def triage_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

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

    # Serialize slides data for JavaScript
    slides_json = json.dumps(
        [
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
        ],
        ensure_ascii=False,
    )

    return templates.TemplateResponse(
        "triage.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
            "report": report,
            "slides_json": slides_json,
        },
    )


@router.get("/reports/{report_id}/review", response_class=HTMLResponse)
async def review_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        return RedirectResponse(url="/")

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
            "report": report,
        },
    )


@router.get("/reports/{report_id}/slides", response_class=HTMLResponse)
async def report_slides_page(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

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
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
            "report": report,
            "slides": slides,
        },
    )


@router.get("/cases", response_class=HTMLResponse)
async def cases_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse(
        "case_list.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
        },
    )


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail_page(
    case_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/auth/login")

    result = await db.execute(select(FACase).where(FACase.id == case_id))
    case = result.scalar_one_or_none()
    if not case:
        return RedirectResponse(url="/cases")

    return templates.TemplateResponse(
        "case_detail.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
            "case": case,
        },
    )


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
        {"report": row[0], "uploader_name": row[1]} for row in reports_result.all()
    ]

    return templates.TemplateResponse(
        "week_detail.html",
        {
            "request": request,
            "user": user,
            "scopes": user.get("scopes", []),
            "period": period,
            "reports": reports,
        },
    )
