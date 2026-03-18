"""Upload and processing routes with SSE progress."""

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.auth import get_or_create_user, require_scope
from app.core.tasks import track_task
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FAReport, FAReportSlide, FAWeeklyPeriod
from app.services.pptx_parser import (
    convert_pptx_to_images,
    extract_slide_texts,
    pre_filter_slides,
)
from app.services.audit import log_action
from app.services.vlm_extractor import classify_slides_batch

router = APIRouter(prefix="/api", tags=["upload"])

# In-memory store for processing progress (report_id → progress events)
_progress_store: dict[int, asyncio.Queue] = {}

# How long to keep a finished queue before evicting (seconds).
# Gives the SSE client time to connect and drain the final event.
_PROGRESS_TTL_SECONDS = 300


async def _evict_progress_after(report_id: int, delay: float) -> None:
    """Remove a progress queue after *delay* seconds if still present."""
    await asyncio.sleep(delay)
    removed = _progress_store.pop(report_id, None)
    if removed is not None:
        logger.debug("Evicted stale progress queue for report {}", report_id)


@router.post("/upload")
async def upload_report(
    file: UploadFile,
    year: int,
    week_number: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    overwrite: bool = False,
):
    """Upload a PPTX weekly report and start processing."""
    payload = require_scope(request, "write")
    user = await get_or_create_user(db, payload)

    if not file.filename or not file.filename.endswith(".pptx"):
        raise HTTPException(status_code=400, detail="Only .pptx files are supported")

    # Get or create weekly period
    result = await db.execute(
        select(FAWeeklyPeriod).where(
            FAWeeklyPeriod.year == year,
            FAWeeklyPeriod.week_number == week_number,
        )
    )
    period = result.scalar_one_or_none()
    if period is None:
        # Calculate week start/end dates (ISO week)
        import datetime

        start = datetime.date.fromisocalendar(year, week_number, 1)
        end = datetime.date.fromisocalendar(year, week_number, 7)
        period = FAWeeklyPeriod(
            year=year, week_number=week_number, start_date=start, end_date=end
        )
        db.add(period)
        await db.flush()

    # Check for duplicate filename in this weekly period
    dup_result = await db.execute(
        select(FAReport).where(
            FAReport.weekly_period_id == period.id,
            FAReport.filename == file.filename,
        )
    )
    existing_report = dup_result.scalar_one_or_none()

    if existing_report and not overwrite:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"檔案 '{file.filename}' 已存在於 {year}-W{week_number:02d}",
                "existing_report_id": existing_report.id,
                "existing_status": existing_report.status,
                "existing_created_at": existing_report.created_at.isoformat(),
            },
        )

    if existing_report and overwrite:
        old_dir = settings.images_path / str(existing_report.id)
        if old_dir.exists():
            shutil.rmtree(old_dir)
        await db.delete(existing_report)
        await db.flush()
        logger.info("Overwriting report {} ({})", existing_report.id, file.filename)

    # Create report record
    report = FAReport(
        weekly_period_id=period.id,
        uploader_id=user.id,
        filename=file.filename,
        status="processing",
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)

    await log_action(
        db,
        user_id=user.id,
        action="upload",
        target_type="report",
        target_id=report.id,
        detail={
            "filename": file.filename,
            "year": year,
            "week": week_number,
            "overwrite": overwrite,
        },
    )
    await db.commit()

    # Save uploaded file (sanitize filename to prevent path traversal)
    safe_filename = Path(file.filename).name
    report_dir = settings.images_path / str(report.id)
    report_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = report_dir / safe_filename

    # Enforce upload size limit
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    written = 0
    with open(pptx_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            written += len(chunk)
            if written > max_bytes:
                break
            f.write(chunk)

    if written > max_bytes:
        pptx_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"檔案大小超過上限 ({settings.max_upload_size_mb} MB)",
        )

    # Create progress queue
    queue: asyncio.Queue = asyncio.Queue()
    _progress_store[report.id] = queue

    # Start background processing (retain reference to prevent GC)
    task = asyncio.create_task(
        _process_report(request.app, report.id, pptx_path, report_dir, queue)
    )
    track_task(task, request.app.state.background_tasks, "upload processing")

    return {"report_id": report.id, "status": "processing"}


@router.get("/upload/{report_id}/progress")
async def progress_stream(report_id: int, request: Request):
    """SSE endpoint for processing progress."""
    require_scope(request, "read")

    queue = _progress_store.get(report_id)
    if queue is None:
        raise HTTPException(
            status_code=404, detail="No active processing for this report"
        )

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                yield {"event": event["type"], "data": json.dumps(event["data"])}
                if event["type"] in ("complete", "error"):
                    break
        finally:
            # Clean up on any exit (normal completion, client disconnect, error)
            _progress_store.pop(report_id, None)

    return EventSourceResponse(event_generator())


@router.get("/upload/{report_id}/results")
async def get_processing_results(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get the extraction results for review (before saving to DB)."""
    require_scope(request, "read")

    result = await db.execute(select(FAReport).where(FAReport.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    # Results are stored in a temp JSON file during processing
    results_path = settings.images_path / str(report_id) / "extraction_results.json"
    if not results_path.exists():
        raise HTTPException(status_code=404, detail="Results not ready yet")

    with open(results_path) as f:
        results = json.load(f)

    return {
        "report_id": report_id,
        "filename": report.filename,
        "total_slides": report.total_slides,
        "slides": results,
    }


async def _process_report(
    app,
    report_id: int,
    pptx_path: Path,
    output_dir: Path,
    queue: asyncio.Queue,
):
    """Background task: parse PPTX → pre-filter → Stage 1 VLM classify → triage."""
    slides_committed = False
    try:
        async with app.state.db_session() as db:
            # Step 1: Extract text for pre-filtering
            await queue.put(
                {"type": "status", "data": {"message": "正在解析投影片文字..."}}
            )
            slide_texts = extract_slide_texts(pptx_path)
            total_slides = len(slide_texts)

            # Update report with total slides
            result = await db.execute(select(FAReport).where(FAReport.id == report_id))
            report = result.scalar_one()
            report.total_slides = total_slides
            await db.commit()

            # Step 2: Convert to images
            await queue.put(
                {"type": "status", "data": {"message": "正在轉換投影片為圖片..."}}
            )
            images = await convert_pptx_to_images(pptx_path, output_dir)

            # Source PPTX no longer needed (PDF + PNGs now in output_dir)
            pptx_path.unlink(missing_ok=True)

            # Step 3: Pre-filter
            await queue.put(
                {"type": "status", "data": {"message": "正在預篩選候選案例頁..."}}
            )
            is_candidate = pre_filter_slides(slide_texts)
            candidate_indices = [i for i, c in enumerate(is_candidate) if c]
            candidate_images = [images[i] for i in candidate_indices if i < len(images)]
            candidate_numbers = [i + 1 for i in candidate_indices]  # 1-based

            await queue.put(
                {
                    "type": "prefilter",
                    "data": {
                        "total_slides": total_slides,
                        "candidate_count": len(candidate_indices),
                        "candidate_slides": candidate_numbers,
                    },
                }
            )

            # Step 4: Stage 1 — VLM classification on candidate pages
            async def on_classify_progress(completed, total, slide_num):
                await queue.put(
                    {
                        "type": "classify_progress",
                        "data": {
                            "completed": completed,
                            "total": total,
                            "current_slide": slide_num,
                        },
                    }
                )

            if candidate_images:
                classify_results = await classify_slides_batch(
                    app.state.vlm_client,
                    candidate_images,
                    candidate_numbers,
                    on_progress=on_classify_progress,
                )
            else:
                classify_results = []

            # Step 5: Persist per-slide records to DB
            case_count = 0
            not_case_count = 0
            error_count = 0

            for i in range(total_slides):
                slide_num = i + 1
                image_path = str(images[i]) if i < len(images) else ""
                relative_path = (
                    str(Path(image_path).relative_to(settings.upload_dir))
                    if image_path
                    else ""
                )

                slide_rec = FAReportSlide(
                    report_id=report_id,
                    slide_number=slide_num,
                    image_path=relative_path,
                    is_candidate=is_candidate[i],
                )

                if not is_candidate[i]:
                    # Skipped by pre-filter
                    slide_rec.classification_status = "pending"
                else:
                    # Find classification result
                    cls_match = next(
                        (r for r in classify_results if r[0] == slide_num), None
                    )
                    if cls_match is None:
                        slide_rec.classification_status = "error"
                        error_count += 1
                    else:
                        _, cls_result, raw_json, error = cls_match
                        slide_rec.vlm_raw_classification = raw_json
                        if error:
                            slide_rec.classification_status = "error"
                            error_count += 1
                        elif cls_result and cls_result.is_case_page:
                            slide_rec.classification_status = "case"
                            slide_rec.classification_confidence = cls_result.confidence
                            case_count += 1
                        else:
                            slide_rec.classification_status = "not_case"
                            slide_rec.classification_confidence = (
                                cls_result.confidence if cls_result else None
                            )
                            not_case_count += 1

                db.add(slide_rec)

            # Update report status to triage
            report.status = "triage"
            await db.commit()
            slides_committed = True

            await queue.put(
                {
                    "type": "complete",
                    "data": {
                        "report_id": report_id,
                        "total_slides": total_slides,
                        "case_count": case_count,
                        "not_case_count": not_case_count,
                        "error_count": error_count,
                    },
                }
            )

    except BaseException as e:
        is_cancel = isinstance(e, asyncio.CancelledError)
        if is_cancel:
            logger.warning("Processing cancelled for report {}", report_id)
        else:
            logger.exception("Processing failed for report {}", report_id)
        await queue.put(
            {
                "type": "error",
                "data": {
                    "message": "處理被取消" if is_cancel else f"處理失敗: {str(e)}"
                },
            }
        )
        # Update report status
        try:
            async with app.state.db_session() as db:
                result = await db.execute(
                    select(FAReport).where(FAReport.id == report_id)
                )
                report = result.scalar_one_or_none()
                if report:
                    report.status = "error"
                    await db.commit()
        except Exception:
            logger.exception("Failed to update report status to error")
        # Clean up generated files if no slide records were committed
        if not slides_committed:
            shutil.rmtree(output_dir, ignore_errors=True)
            logger.info("Cleaned up files for failed report {}", report_id)
        if is_cancel:
            raise
    finally:
        # Schedule cleanup so the queue doesn't leak if no SSE client drains it
        evict_task = asyncio.create_task(
            _evict_progress_after(report_id, _PROGRESS_TTL_SECONDS)
        )
        track_task(evict_task, app.state.background_tasks, "progress eviction")
