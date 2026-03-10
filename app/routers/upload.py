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

from app.core.auth import get_current_user_payload, get_or_create_user
from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FAReport, FAWeeklyPeriod
from app.services.data_cleaner import clean_extracted_data
from app.services.pptx_parser import (
    convert_pptx_to_images,
    extract_slide_texts,
    pre_filter_slides,
)
from app.services.vlm_extractor import extract_slides_batch
router = APIRouter(prefix="/api", tags=["upload"])

# In-memory store for processing progress (report_id → progress events)
_progress_store: dict[int, asyncio.Queue] = {}


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
    payload = get_current_user_payload(request)
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

    # Save uploaded file
    report_dir = settings.images_path / str(report.id)
    report_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = report_dir / file.filename

    with open(pptx_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Create progress queue
    queue: asyncio.Queue = asyncio.Queue()
    _progress_store[report.id] = queue

    # Start background processing
    asyncio.create_task(_process_report(request.app, report.id, pptx_path, report_dir, queue))

    return {"report_id": report.id, "status": "processing"}


@router.get("/upload/{report_id}/progress")
async def progress_stream(report_id: int):
    """SSE endpoint for processing progress."""
    queue = _progress_store.get(report_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="No active processing for this report")

    async def event_generator():
        while True:
            event = await queue.get()
            yield {"event": event["type"], "data": json.dumps(event["data"])}
            if event["type"] in ("complete", "error"):
                _progress_store.pop(report_id, None)
                break

    return EventSourceResponse(event_generator())


@router.get("/upload/{report_id}/results")
async def get_processing_results(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get the extraction results for review (before saving to DB)."""
    get_current_user_payload(request)

    result = await db.execute(
        select(FAReport).where(FAReport.id == report_id)
    )
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
    """Background task: parse PPTX → pre-filter → VLM extract → save results."""
    try:
        async with app.state.db_session() as db:
            # Step 1: Extract text for pre-filtering
            await queue.put({"type": "status", "data": {"message": "正在解析投影片文字..."}})
            slide_texts = extract_slide_texts(pptx_path)
            total_slides = len(slide_texts)

            # Update report with total slides
            result = await db.execute(
                select(FAReport).where(FAReport.id == report_id)
            )
            report = result.scalar_one()
            report.total_slides = total_slides
            await db.commit()

            # Step 2: Convert to images
            await queue.put({"type": "status", "data": {"message": "正在轉換投影片為圖片..."}})
            images = await convert_pptx_to_images(pptx_path, output_dir)

            # Step 3: Pre-filter
            await queue.put({"type": "status", "data": {"message": "正在預篩選候選案例頁..."}})
            is_candidate = pre_filter_slides(slide_texts)
            candidate_indices = [i for i, c in enumerate(is_candidate) if c]
            candidate_images = [images[i] for i in candidate_indices if i < len(images)]
            candidate_numbers = [i + 1 for i in candidate_indices]  # 1-based

            await queue.put({
                "type": "prefilter",
                "data": {
                    "total_slides": total_slides,
                    "candidate_count": len(candidate_indices),
                    "candidate_slides": candidate_numbers,
                },
            })

            # Step 4: VLM extraction on candidate pages
            async def on_vlm_progress(completed, total, slide_num):
                await queue.put({
                    "type": "vlm_progress",
                    "data": {
                        "completed": completed,
                        "total": total,
                        "current_slide": slide_num,
                    },
                })

            if candidate_images:
                vlm_results = await extract_slides_batch(
                    app.state.vlm_client, candidate_images, candidate_numbers, on_progress=on_vlm_progress
                )
            else:
                vlm_results = []

            # Step 5: Clean data and build results
            all_slides = []
            for i in range(total_slides):
                slide_num = i + 1
                image_path = str(images[i]) if i < len(images) else ""
                relative_path = str(Path(image_path).relative_to(settings.upload_dir)) if image_path else ""

                if not is_candidate[i]:
                    all_slides.append({
                        "slide_number": slide_num,
                        "image_path": relative_path,
                        "is_case_page": False,
                        "skipped": True,
                        "data": None,
                        "error": None,
                    })
                    continue

                # Find VLM result for this slide
                vlm_match = next(
                    (r for r in vlm_results if r[0] == slide_num), None
                )
                if vlm_match is None:
                    all_slides.append({
                        "slide_number": slide_num,
                        "image_path": relative_path,
                        "is_case_page": False,
                        "skipped": False,
                        "data": None,
                        "error": "No VLM result",
                    })
                    continue

                _, vlm_result, raw_response, error = vlm_match
                if error:
                    all_slides.append({
                        "slide_number": slide_num,
                        "image_path": relative_path,
                        "is_case_page": False,
                        "skipped": False,
                        "data": None,
                        "error": error,
                        "raw_vlm_response": raw_response,
                    })
                elif vlm_result and vlm_result.is_case_page and vlm_result.data:
                    cleaned = clean_extracted_data(vlm_result.data)
                    all_slides.append({
                        "slide_number": slide_num,
                        "image_path": relative_path,
                        "is_case_page": True,
                        "skipped": False,
                        "data": cleaned,
                        "error": None,
                        "raw_vlm_response": raw_response,
                    })
                else:
                    all_slides.append({
                        "slide_number": slide_num,
                        "image_path": relative_path,
                        "is_case_page": False,
                        "skipped": False,
                        "data": None,
                        "error": None,
                        "raw_vlm_response": raw_response,
                    })

            # Save results to temp file for review
            results_path = output_dir / "extraction_results.json"
            with open(results_path, "w") as f:
                json.dump(all_slides, f, ensure_ascii=False, indent=2)

            # Update report status to review
            report.status = "review"
            await db.commit()

            await queue.put({
                "type": "complete",
                "data": {
                    "report_id": report_id,
                    "total_slides": total_slides,
                    "case_pages": sum(1 for s in all_slides if s["is_case_page"]),
                },
            })

    except Exception as e:
        logger.exception("Processing failed for report {}", report_id)
        await queue.put({
            "type": "error",
            "data": {"message": f"處理失敗: {str(e)}"},
        })
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
