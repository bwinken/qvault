"""Case CRUD, review confirmation, and search routes."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user_payload, get_or_create_user
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FACase, FAReport, FAWeeklyPeriod
from app.schemas.fa_case import CaseEditRequest
from app.services.embedding import build_case_text, generate_embeddings_for_case, generate_text_embedding

router = APIRouter(prefix="/api", tags=["cases"])


@router.post("/reports/{report_id}/confirm")
async def confirm_and_save(
    report_id: int,
    cases_data: list[dict],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Save reviewed cases to DB (先審後存). Called after user reviews extraction results."""
    get_current_user_payload(request)

    result = await db.execute(
        select(FAReport).where(FAReport.id == report_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status != "review":
        raise HTTPException(status_code=400, detail="Report is not in review status")

    created_cases = []
    for case_data in cases_data:
        slide_number = case_data.pop("slide_number", 0)
        image_path = case_data.pop("image_path", "")
        raw_vlm = case_data.pop("raw_vlm_response", None)

        case = FACase(
            report_id=report_id,
            slide_number=slide_number,
            slide_image_path=image_path,
            date=case_data.get("date"),
            customer=case_data.get("customer"),
            device=case_data.get("device"),
            model=case_data.get("model"),
            defect_mode=case_data.get("defect_mode"),
            defect_rate_raw=case_data.get("defect_rate_raw"),
            defect_lots=case_data.get("defect_lots", []),
            fab_assembly=case_data.get("fab_assembly"),
            fa_status=case_data.get("fa_status"),
            follow_up=case_data.get("follow_up"),
            raw_vlm_response=raw_vlm,
        )
        db.add(case)
        created_cases.append(case)

    report.status = "done"
    await db.commit()

    # Generate embeddings in background (don't block the response)
    vlm_client = request.app.state.vlm_client
    for case in created_cases:
        await db.refresh(case)
        try:
            text_emb, image_emb = await generate_embeddings_for_case(vlm_client, case)
            if text_emb:
                case.text_embedding = text_emb
            if image_emb:
                case.image_embedding = image_emb
            await db.commit()
        except Exception as e:
            logger.warning("Embedding generation failed for case {}: {}", case.id, e)

    return {"status": "saved", "case_count": len(created_cases)}


@router.get("/cases")
async def list_cases(
    request: Request,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, description="Full-text search query"),
    customer: str | None = Query(None),
    device: str | None = Query(None),
    year: int | None = Query(None),
    week: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List/search FA cases with filtering and full-text search."""
    get_current_user_payload(request)

    query = select(FACase).join(FAReport).join(FAWeeklyPeriod)

    # Filters
    if customer:
        query = query.where(FACase.customer.ilike(f"%{customer}%"))
    if device:
        query = query.where(FACase.device.ilike(f"%{device}%"))
    if year:
        query = query.where(FAWeeklyPeriod.year == year)
    if week:
        query = query.where(FAWeeklyPeriod.week_number == week)

    # Full-text search
    if q:
        ts_query = func.plainto_tsquery("simple", q)
        ts_vector = text(
            "to_tsvector('simple', "
            "coalesce(fa_cases.customer,'') || ' ' || "
            "coalesce(fa_cases.device,'') || ' ' || "
            "coalesce(fa_cases.model,'') || ' ' || "
            "coalesce(fa_cases.defect_mode,'') || ' ' || "
            "coalesce(fa_cases.fa_status,'') || ' ' || "
            "coalesce(fa_cases.follow_up,''))"
        )
        query = query.where(ts_vector.op("@@")(ts_query))

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    # Paginate
    query = query.order_by(FACase.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    cases = result.scalars().all()

    return {
        "items": [
            {
                "id": c.id,
                "report_id": c.report_id,
                "slide_number": c.slide_number,
                "slide_image_path": c.slide_image_path,
                "date": c.date,
                "customer": c.customer,
                "device": c.device,
                "model": c.model,
                "defect_mode": c.defect_mode,
                "defect_rate_raw": c.defect_rate_raw,
                "defect_lots": c.defect_lots,
                "fab_assembly": c.fab_assembly,
                "fa_status": c.fa_status,
                "follow_up": c.follow_up,
                "created_at": c.created_at.isoformat(),
            }
            for c in cases
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/cases/{case_id}")
async def get_case(
    case_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get a single case detail."""
    get_current_user_payload(request)

    result = await db.execute(
        select(FACase).where(FACase.id == case_id)
    )
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    return {
        "id": case.id,
        "report_id": case.report_id,
        "slide_number": case.slide_number,
        "slide_image_path": case.slide_image_path,
        "date": case.date,
        "customer": case.customer,
        "device": case.device,
        "model": case.model,
        "defect_mode": case.defect_mode,
        "defect_rate_raw": case.defect_rate_raw,
        "defect_lots": case.defect_lots,
        "fab_assembly": case.fab_assembly,
        "fa_status": case.fa_status,
        "follow_up": case.follow_up,
        "raw_vlm_response": case.raw_vlm_response,
        "created_at": case.created_at.isoformat(),
    }


@router.put("/cases/{case_id}")
async def update_case(
    case_id: int,
    data: CaseEditRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update a case's fields."""
    get_current_user_payload(request)

    result = await db.execute(
        select(FACase).where(FACase.id == case_id)
    )
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(case, field, value)

    await db.commit()

    # Re-generate text embedding after update
    try:
        text = build_case_text(case)
        if text:
            case.text_embedding = await generate_text_embedding(request.app.state.vlm_client, text)
            await db.commit()
    except Exception as e:
        logger.warning("Failed to update embedding for case {}: {}", case_id, e)

    return {"status": "updated"}


@router.delete("/cases/{case_id}")
async def delete_case(
    case_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Delete a case."""
    get_current_user_payload(request)

    result = await db.execute(
        select(FACase).where(FACase.id == case_id)
    )
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    await db.delete(case)
    await db.commit()
    return {"status": "deleted"}


@router.get("/weeks")
async def list_weeks(
    request: Request,
    db: AsyncSession = Depends(get_db),
    year: int | None = Query(None),
):
    """List weekly periods with report/case counts."""
    get_current_user_payload(request)

    query = (
        select(
            FAWeeklyPeriod,
            func.count(func.distinct(FAReport.id)).label("report_count"),
            func.count(FACase.id).label("case_count"),
        )
        .outerjoin(FAReport, FAWeeklyPeriod.id == FAReport.weekly_period_id)
        .outerjoin(FACase, FAReport.id == FACase.report_id)
        .group_by(FAWeeklyPeriod.id)
        .order_by(FAWeeklyPeriod.year.desc(), FAWeeklyPeriod.week_number.desc())
    )

    if year:
        query = query.where(FAWeeklyPeriod.year == year)

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": row[0].id,
            "year": row[0].year,
            "week_number": row[0].week_number,
            "start_date": row[0].start_date.isoformat(),
            "end_date": row[0].end_date.isoformat(),
            "report_count": row[1],
            "case_count": row[2],
        }
        for row in rows
    ]
