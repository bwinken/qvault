"""Centralized logging configuration using loguru."""

import logging
import sys

from loguru import logger

from app.core.config import settings


class _InterceptHandler(logging.Handler):
    """Route stdlib logging into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where the logged message originated
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """Configure loguru as the sole logging backend."""
    # Remove default loguru handler
    logger.remove()

    # Console handler with colored output
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level="DEBUG" if settings.dev_skip_auth else "INFO",
        colorize=True,
    )

    # Rotating file handler
    logger.add(
        "logs/app.log",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        encoding="utf-8",
    )

    # Intercept stdlib logging → loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Quiet noisy third-party loggers
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
