"""Triage routes: classification confirmation, Stage 2 extraction trigger, reclassify."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_scope
from app.core.tasks import track_task
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FAReport, FAReportSlide
from app.routers.upload import (
    _PROGRESS_TTL_SECONDS,
    _evict_progress_after,
    _progress_store,
)
from app.schemas.fa_case import SlideTriageInfo, TriageConfirmRequest
from app.services.data_cleaner import clean_extracted_data
from app.services.vlm_extractor import (
    classify_single_slide,
    extract_slides_batch,
)

router = APIRouter(prefix="/api", tags=["triage"])


@router.get("/reports/{report_id}/triage")
async def get_triage_data(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get classification results for triage UI."""
    require_scope(request, "read")

    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    slides_result = await db.execute(
        select(FAReportSlide)
        .where(FAReportSlide.report_id == report_id)
        .order_by(FAReportSlide.slide_number)
    )
    slides = slides_result.scalars().all()

    return {
        "report_id": report_id,
        "filename": report.filename,
        "total_slides": report.total_slides,
        "status": report.status,
        "slides": [
            SlideTriageInfo(
                id=s.id,
                slide_number=s.slide_number,
                image_path=s.image_path,
                is_candidate=s.is_candidate,
                classification_status=s.classification_status,
                classification_confidence=s.classification_confidence,
                is_case_page=s.is_case_page,
            ).model_dump()
            for s in slides
        ],
    }


@router.post("/reports/{report_id}/triage/confirm")
async def confirm_triage(
    report_id: int,
    body: TriageConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Confirm user's classification overrides before triggering extraction."""
    require_scope(request, "write")

    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status not in ("triage", "review"):
        raise HTTPException(
            status_code=400, detail=f"Report status '{report.status}' cannot be triaged"
        )

    # Apply user classifications
    for cls in body.classifications:
        slide_result = await db.execute(
            select(FAReportSlide).where(FAReportSlide.id == cls.slide_id)
        )
        slide = slide_result.scalar_one_or_none()
        if slide and slide.report_id == report_id:
            slide.is_case_page = cls.is_case_page

    await db.commit()

    # Count confirmed case pages
    case_result = await db.execute(
        select(FAReportSlide).where(
            FAReportSlide.report_id == report_id,
            FAReportSlide.is_case_page.is_(True),
        )
    )
    case_count = len(case_result.scalars().all())

    return {"status": "confirmed", "case_count": case_count}


@router.post("/reports/{report_id}/extract")
async def trigger_extraction(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Trigger Stage 2 field extraction for confirmed case slides."""
    require_scope(request, "write")

    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status not in ("triage",):
        raise HTTPException(
            status_code=400,
            detail=f"Report status '{report.status}' cannot start extraction",
        )

    # Find confirmed case slides that need extraction
    slides_result = await db.execute(
        select(FAReportSlide).where(
            FAReportSlide.report_id == report_id,
            FAReportSlide.is_case_page.is_(True),
            FAReportSlide.extraction_status == "pending",
        )
    )
    case_slides = slides_result.scalars().all()

    if not case_slides:
        raise HTTPException(status_code=400, detail="No case slides to extract")

    # Set status to extracting
    report.status = "extracting"
    await db.commit()

    # Create SSE progress queue
    queue: asyncio.Queue = asyncio.Queue()
    _progress_store[report_id] = queue

    # Start background extraction (retain reference to prevent GC)
    task = asyncio.create_task(
        _run_extraction(request.app, report_id, case_slides, queue)
    )
    track_task(task, request.app.state.background_tasks, "extraction")

    return {
        "report_id": report_id,
        "status": "extracting",
        "slide_count": len(case_slides),
    }


async def _run_extraction(
    app,
    report_id: int,
    case_slides: list[FAReportSlide],
    queue: asyncio.Queue,
):
    """Background task: Stage 2 VLM extraction on confirmed case slides."""
    try:
        # Prepare image paths and slide numbers
        upload_dir = Path(settings.upload_dir)
        image_paths = [upload_dir / s.image_path for s in case_slides if s.image_path]
        slide_numbers = [s.slide_number for s in case_slides if s.image_path]

        async def on_extract_progress(completed, total, slide_num):
            await queue.put(
                {
                    "type": "extract_progress",
                    "data": {
                        "completed": completed,
                        "total": total,
                        "current_slide": slide_num,
                    },
                }
            )

        extract_results = await extract_slides_batch(
            app.state.vlm_client,
            image_paths,
            slide_numbers,
            on_progress=on_extract_progress,
        )

        # Build extraction results and update slide records
        async with app.state.db_session() as db:
            all_extraction = []
            for slide_num, vlm_result, raw_json, error in extract_results:
                slide_result = await db.execute(
                    select(FAReportSlide).where(
                        FAReportSlide.report_id == report_id,
                        FAReportSlide.slide_number == slide_num,
                    )
                )
                slide_rec = slide_result.scalar_one_or_none()
                if not slide_rec:
                    continue

                if error:
                    slide_rec.extraction_status = "error"
                    all_extraction.append(
                        {
                            "slide_number": slide_num,
                            "image_path": slide_rec.image_path,
                            "is_case_page": True,
                            "skipped": False,
                            "data": None,
                            "error": error,
                            "raw_vlm_response": raw_json,
                        }
                    )
                elif vlm_result and vlm_result.data:
                    cleaned = clean_extracted_data(vlm_result.data)
                    slide_rec.extraction_status = "done"
                    all_extraction.append(
                        {
                            "slide_number": slide_num,
                            "image_path": slide_rec.image_path,
                            "is_case_page": True,
                            "skipped": False,
                            "data": cleaned,
                            "error": None,
                            "raw_vlm_response": raw_json,
                        }
                    )
                else:
                    slide_rec.extraction_status = "error"
                    all_extraction.append(
                        {
                            "slide_number": slide_num,
                            "image_path": slide_rec.image_path,
                            "is_case_page": True,
                            "skipped": False,
                            "data": None,
                            "error": "VLM returned no data",
                            "raw_vlm_response": raw_json,
                        }
                    )

            # Save extraction results for review page
            output_dir = settings.images_path / str(report_id)
            results_path = output_dir / "extraction_results.json"
            with open(results_path, "w") as f:
                json.dump(all_extraction, f, ensure_ascii=False, indent=2)

            # Update report status
            report_result = await db.execute(
                select(FAReport).where(FAReport.id == report_id)
            )
            report = report_result.scalar_one()
            report.status = "review"
            await db.commit()

            extracted_count = sum(1 for s in all_extraction if s["data"] is not None)
            await queue.put(
                {
                    "type": "complete",
                    "data": {
                        "report_id": report_id,
                        "extracted_count": extracted_count,
                        "error_count": len(all_extraction) - extracted_count,
                    },
                }
            )

    except BaseException as e:
        is_cancel = isinstance(e, asyncio.CancelledError)
        if is_cancel:
            logger.warning("Extraction cancelled for report {}", report_id)
        else:
            logger.exception("Extraction failed for report {}", report_id)
        await queue.put(
            {
                "type": "error",
                "data": {
                    "message": "提取被取消" if is_cancel else f"提取失敗: {str(e)}"
                },
            }
        )
        try:
            async with app.state.db_session() as db:
                result = await db.execute(
                    select(FAReport).where(FAReport.id == report_id)
                )
                report = result.scalar_one_or_none()
                if report:
                    report.status = "error" if is_cancel else "triage"
                    await db.commit()
        except Exception:
            logger.exception("Failed to revert report status after extraction error")
        if is_cancel:
            raise
    finally:
        # Schedule cleanup so the queue doesn't leak if no SSE client drains it
        evict_task = asyncio.create_task(
            _evict_progress_after(report_id, _PROGRESS_TTL_SECONDS)
        )
        track_task(evict_task, app.state.background_tasks, "progress eviction")


@router.post("/slides/{slide_id}/reclassify")
async def reclassify_slide(
    slide_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Retry VLM classification for a single slide."""
    require_scope(request, "write")

    result = await db.execute(select(FAReportSlide).where(FAReportSlide.id == slide_id))
    slide = result.scalar_one_or_none()
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")

    if not slide.image_path:
        raise HTTPException(status_code=400, detail="Slide has no image")

    image_path = Path(settings.upload_dir) / slide.image_path

    try:
        cls_result = await classify_single_slide(
            request.app.state.vlm_client,
            image_path,
            slide.slide_number,
        )
        slide.classification_status = "case" if cls_result.is_case_page else "not_case"
        slide.classification_confidence = cls_result.confidence
        slide.vlm_raw_classification = cls_result.model_dump_json()
        await db.commit()

        return {
            "slide_id": slide_id,
            "classification_status": slide.classification_status,
            "confidence": cls_result.confidence,
            "reason": cls_result.reason,
        }
    except Exception as e:
        slide.classification_status = "error"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Classification failed: {str(e)}")
