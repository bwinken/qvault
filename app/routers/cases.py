"""Case CRUD, review confirmation, and search routes."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_or_create_user, require_scope
from app.core.tasks import track_task
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import (
    FACase,
    FACaseFieldLog,
    FAReport,
    FAReportSlide,
    FAUser,
    FAWeeklyPeriod,
)
from app.schemas.fa_case import CaseEditRequest, ConfirmCaseData, SimilarCaseResult
from app.services.audit import log_action
from app.services.embedding import (
    build_case_text,
    generate_embeddings_for_case,
    generate_text_embedding,
)
from app.services.weekly_summary import generate_weekly_summary

router = APIRouter(prefix="/api", tags=["cases"])


@router.post("/reports/{report_id}/confirm")
async def confirm_and_save(
    report_id: int,
    cases_data: list[ConfirmCaseData],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Save reviewed cases to DB (先審後存). Called after user reviews extraction results."""
    payload = require_scope(request, "write")
    user = await get_or_create_user(db, payload)

    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status != "review":
        raise HTTPException(status_code=400, detail="Report is not in review status")

    created_cases = []
    for case_data in cases_data:
        case = FACase(
            report_id=report_id,
            confirmed_by_id=user.id,
            slide_number=case_data.slide_number,
            slide_image_path=case_data.image_path,
            date=case_data.date,
            customer=case_data.customer,
            device=case_data.device,
            model=case_data.model,
            defect_mode=case_data.defect_mode,
            defect_rate_raw=case_data.defect_rate_raw,
            defect_lots=case_data.defect_lots or [],
            fab_assembly=case_data.fab_assembly,
            fa_status=case_data.fa_status,
            follow_up=case_data.follow_up,
            raw_vlm_response=case_data.raw_vlm_response,
        )
        db.add(case)
        created_cases.append(case)

    report.status = "done"
    await db.commit()

    # Audit + link slide records to confirmed cases
    for case in created_cases:
        await db.refresh(case)
        await log_action(
            db,
            user_id=user.id,
            action="confirm",
            target_type="case",
            target_id=case.id,
            detail={"report_id": report_id, "slide_number": case.slide_number},
        )
        # Link the corresponding slide record
        slide_result = await db.execute(
            select(FAReportSlide).where(
                FAReportSlide.report_id == report_id,
                FAReportSlide.slide_number == case.slide_number,
            )
        )
        slide_rec = slide_result.scalar_one_or_none()
        if slide_rec:
            slide_rec.is_case_page = True
            slide_rec.linked_case_id = case.id
    await db.commit()

    # Clean up temporary extraction results — data is now in the DB
    results_path = settings.images_path / str(report_id) / "extraction_results.json"
    results_path.unlink(missing_ok=True)

    # Generate embeddings + weekly summary in background (don't block the response)
    case_ids = [c.id for c in created_cases]
    weekly_period_id = report.weekly_period_id
    task = asyncio.create_task(
        _generate_embeddings_background(request.app, case_ids, weekly_period_id)
    )
    track_task(task, request.app.state.background_tasks, "embedding generation")

    return {"status": "saved", "case_count": len(created_cases)}


async def _generate_embeddings_background(
    app, case_ids: list[int], weekly_period_id: int | None
):
    """Background task: generate embeddings for confirmed cases + optional weekly summary."""
    async with app.state.db_session() as db:
        for case_id in case_ids:
            try:
                result = await db.execute(select(FACase).where(FACase.id == case_id))
                case = result.scalar_one_or_none()
                if not case:
                    continue
                text_emb, image_emb = await generate_embeddings_for_case(
                    app.state.vlm_client, case
                )
                if text_emb:
                    case.text_embedding = text_emb
                if image_emb:
                    case.image_embedding = image_emb
                await db.commit()
            except Exception as e:
                logger.warning(
                    "Embedding generation failed for case {}: {}", case_id, e
                )

        if weekly_period_id is not None:
            try:
                await generate_weekly_summary(
                    app.state.vlm_client, db, weekly_period_id
                )
            except Exception as e:
                logger.warning("Weekly summary generation failed: {}", e)


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
    require_scope(request, "read")

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
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
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
    require_scope(request, "read")

    result = await db.execute(select(FACase).where(FACase.id == case_id))
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
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
    }


@router.get("/cases/{case_id}/history")
async def get_case_history(
    case_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get field-level edit history for a case."""
    require_scope(request, "read")

    result = await db.execute(
        select(FACaseFieldLog, FAUser.employee_name)
        .join(FAUser, FACaseFieldLog.edited_by_id == FAUser.id)
        .where(FACaseFieldLog.case_id == case_id)
        .order_by(FACaseFieldLog.edited_at.desc())
        .limit(100)
    )
    rows = result.all()

    return [
        {
            "id": log.id,
            "field_name": log.field_name,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "edited_by": name,
            "edited_at": log.edited_at.isoformat(),
        }
        for log, name in rows
    ]


@router.put("/cases/{case_id}")
async def update_case(
    case_id: int,
    data: CaseEditRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update a case's fields."""
    payload = require_scope(request, "write")
    user = await get_or_create_user(db, payload)

    result = await db.execute(select(FACase).where(FACase.id == case_id))
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    update_data = data.model_dump(exclude_unset=True)
    changes = {
        field: {"old": getattr(case, field), "new": value}
        for field, value in update_data.items()
        if getattr(case, field) != value
    }
    for field, value in update_data.items():
        setattr(case, field, value)

    if changes:
        case.updated_at = datetime.now(timezone.utc)
        case.updated_by_id = user.id

        # Write field-level change logs
        for field, change in changes.items():
            old_val = change["old"]
            new_val = change["new"]
            if isinstance(old_val, list):
                old_val = ", ".join(old_val) if old_val else None
            if isinstance(new_val, list):
                new_val = ", ".join(new_val) if new_val else None
            db.add(
                FACaseFieldLog(
                    case_id=case_id,
                    field_name=field,
                    old_value=str(old_val) if old_val is not None else None,
                    new_value=str(new_val) if new_val is not None else None,
                    edited_by_id=user.id,
                )
            )

    await log_action(
        db,
        user_id=user.id,
        action="edit",
        target_type="case",
        target_id=case_id,
        detail=changes if changes else None,
    )
    await db.commit()

    # Re-generate text embedding after update
    try:
        text = build_case_text(case)
        if text:
            case.text_embedding = await generate_text_embedding(
                request.app.state.vlm_client, text
            )
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
    payload = require_scope(request, "write")
    user = await get_or_create_user(db, payload)

    result = await db.execute(select(FACase).where(FACase.id == case_id))
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Audit before delete (capture key fields for the record)
    await log_action(
        db,
        user_id=user.id,
        action="delete",
        target_type="case",
        target_id=case_id,
        detail={
            "report_id": case.report_id,
            "customer": case.customer,
            "device": case.device,
            "defect_mode": case.defect_mode,
        },
    )

    # Unlink from slide record
    slide_result = await db.execute(
        select(FAReportSlide).where(
            FAReportSlide.report_id == case.report_id,
            FAReportSlide.linked_case_id == case_id,
        )
    )
    slide_rec = slide_result.scalar_one_or_none()
    if slide_rec:
        slide_rec.is_case_page = False
        slide_rec.linked_case_id = None

    await db.delete(case)
    await db.commit()
    return {"status": "deleted"}


@router.get("/reports/{report_id}/slides")
async def list_report_slides(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all slides for a report with their case linkage status."""
    require_scope(request, "read")

    result = await db.execute(
        select(FAReportSlide)
        .where(FAReportSlide.report_id == report_id)
        .order_by(FAReportSlide.slide_number)
    )
    slides = result.scalars().all()
    if not slides:
        raise HTTPException(status_code=404, detail="No slides found for this report")

    return [
        {
            "id": s.id,
            "slide_number": s.slide_number,
            "image_path": s.image_path,
            "is_candidate": s.is_candidate,
            "is_case_page": s.is_case_page,
            "linked_case_id": s.linked_case_id,
        }
        for s in slides
    ]


@router.post("/slides/{slide_id}/create-case")
async def create_case_from_slide(
    slide_id: int,
    data: CaseEditRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Manually create an FA case from any slide (post-confirmation recovery)."""
    payload = require_scope(request, "write")
    user = await get_or_create_user(db, payload)

    result = await db.execute(select(FAReportSlide).where(FAReportSlide.id == slide_id))
    slide = result.scalar_one_or_none()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    if slide.linked_case_id is not None:
        raise HTTPException(status_code=409, detail="Slide already linked to a case")

    case = FACase(
        report_id=slide.report_id,
        confirmed_by_id=user.id,
        slide_number=slide.slide_number,
        slide_image_path=slide.image_path,
        date=data.date,
        customer=data.customer,
        device=data.device,
        model=data.model,
        defect_mode=data.defect_mode,
        defect_rate_raw=data.defect_rate_raw,
        defect_lots=data.defect_lots,
        fab_assembly=data.fab_assembly,
        fa_status=data.fa_status,
        follow_up=data.follow_up,
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)

    # Link slide → case
    slide.is_case_page = True
    slide.linked_case_id = case.id

    await log_action(
        db,
        user_id=user.id,
        action="confirm",
        target_type="case",
        target_id=case.id,
        detail={
            "report_id": slide.report_id,
            "slide_number": slide.slide_number,
            "source": "manual_recovery",
        },
    )
    await db.commit()

    # Generate embeddings
    try:
        vlm_client = request.app.state.vlm_client
        text_emb, image_emb = await generate_embeddings_for_case(vlm_client, case)
        if text_emb:
            case.text_embedding = text_emb
        if image_emb:
            case.image_embedding = image_emb
        await db.commit()
    except Exception as e:
        logger.warning("Embedding generation failed for case {}: {}", case.id, e)

    return {"status": "created", "case_id": case.id}


@router.get("/cases/search/similar")
async def search_similar_cases(
    request: Request,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, description="Text query for semantic search"),
    case_id: int | None = Query(None, description="Find cases similar to this case"),
    limit: int = Query(10, ge=1, le=50),
):
    """Find similar cases using pgvector cosine similarity.

    Provide either `q` (text query → generate embedding → search)
    or `case_id` (use existing case's text_embedding as query vector).
    """
    require_scope(request, "read")

    if not q and not case_id:
        raise HTTPException(status_code=400, detail="Provide either 'q' or 'case_id'")

    query_embedding = None

    if case_id:
        # Use an existing case's embedding as the query vector
        result = await db.execute(
            select(FACase.text_embedding).where(FACase.id == case_id)
        )
        row = result.first()
        if not row or row[0] is None:
            raise HTTPException(
                status_code=404, detail="Case not found or has no embedding"
            )
        query_embedding = row[0]
    else:
        # Generate embedding from text query
        vlm_client = request.app.state.vlm_client
        try:
            query_embedding = await generate_text_embedding(vlm_client, q)
        except Exception as e:
            logger.error("Failed to generate query embedding: {}", e)
            raise HTTPException(status_code=502, detail="Embedding service unavailable")

    # Cosine distance: <=> operator (lower = more similar)
    cosine_dist = FACase.text_embedding.cosine_distance(query_embedding)

    stmt = (
        select(FACase, cosine_dist.label("distance"))
        .where(FACase.text_embedding.isnot(None))
        .order_by(cosine_dist)
        .limit(limit)
    )

    # Exclude the query case itself from results
    if case_id:
        stmt = stmt.where(FACase.id != case_id)

    result = await db.execute(stmt)
    rows = result.all()

    return [
        SimilarCaseResult(
            id=case.id,
            report_id=case.report_id,
            slide_number=case.slide_number,
            date=case.date,
            customer=case.customer,
            device=case.device,
            model=case.model,
            defect_mode=case.defect_mode,
            fa_status=case.fa_status,
            similarity=round(1 - distance, 4),  # convert distance → similarity
        )
        for case, distance in rows
    ]


@router.post("/cases/regenerate-embeddings")
async def regenerate_missing_embeddings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200, description="Max cases to process"),
):
    """Find confirmed cases with missing text embeddings and regenerate them."""
    require_scope(request, "admin")

    result = await db.execute(
        select(FACase.id)
        .where(FACase.confirmed_by_id.isnot(None))
        .where(FACase.text_embedding.is_(None))
        .limit(limit)
    )
    case_ids = [row[0] for row in result.all()]

    if not case_ids:
        return {"status": "ok", "message": "No cases with missing embeddings"}

    task = asyncio.create_task(
        _generate_embeddings_background(request.app, case_ids, weekly_period_id=None)
    )
    track_task(task, request.app.state.background_tasks, "embedding regeneration")

    return {"status": "queued", "case_count": len(case_ids)}


@router.post("/admin/archive-vlm-responses")
async def archive_old_vlm_responses(
    request: Request,
    db: AsyncSession = Depends(get_db),
    days: int = Query(90, ge=7, description="Archive responses older than N days"),
):
    """Null out raw_vlm_response for old confirmed cases to reclaim DB space.

    This is a maintenance endpoint — raw VLM responses are only useful for
    debugging shortly after extraction.  After *days* have passed the
    structured fields in the case record are the source of truth.
    """
    require_scope(request, "admin")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        update(FACase)
        .where(FACase.created_at < cutoff)
        .where(FACase.raw_vlm_response.isnot(None))
        .values(raw_vlm_response=None)
    )
    await db.commit()
    return {"status": "archived", "archived_count": result.rowcount}


@router.get("/weeks")
async def list_weeks(
    request: Request,
    db: AsyncSession = Depends(get_db),
    year: int | None = Query(None),
):
    """List weekly periods with report/case counts."""
    require_scope(request, "read")

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
