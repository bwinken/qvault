import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class FAUser(Base):
    __tablename__ = "fa_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_name: Mapped[str] = mapped_column(Text, unique=True)  # JWT sub
    org_id: Mapped[str | None] = mapped_column(Text)  # JWT org_id
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    reports: Mapped[list["FAReport"]] = relationship(back_populates="uploader")


class FAWeeklyPeriod(Base):
    __tablename__ = "fa_weekly_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    week_number: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime.date] = mapped_column(Date)
    end_date: Mapped[datetime.date] = mapped_column(Date)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    reports: Mapped[list["FAReport"]] = relationship(back_populates="weekly_period")

    __table_args__ = (
        Index("idx_weekly_year_week", "year", "week_number", unique=True),
    )


class FAReport(Base):
    __tablename__ = "fa_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    weekly_period_id: Mapped[int] = mapped_column(
        ForeignKey("fa_weekly_periods.id", ondelete="CASCADE")
    )
    uploader_id: Mapped[int] = mapped_column(
        ForeignKey("fa_users.id", ondelete="CASCADE")
    )
    filename: Mapped[str]
    total_slides: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="processing")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    weekly_period: Mapped["FAWeeklyPeriod"] = relationship(back_populates="reports")
    uploader: Mapped["FAUser"] = relationship(back_populates="reports")
    cases: Mapped[list["FACase"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class FACase(Base):
    __tablename__ = "fa_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("fa_reports.id", ondelete="CASCADE")
    )
    slide_number: Mapped[int]
    slide_image_path: Mapped[str | None]

    date: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[str | None] = mapped_column(Text)
    device: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    defect_mode: Mapped[str | None] = mapped_column(Text)
    defect_rate_raw: Mapped[str | None] = mapped_column(Text)
    defect_lots: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    fab_assembly: Mapped[str | None] = mapped_column(Text)
    fa_status: Mapped[str | None] = mapped_column(Text)
    follow_up: Mapped[str | None] = mapped_column(Text)

    raw_vlm_response: Mapped[str | None] = mapped_column(Text)
    text_embedding = mapped_column(Vector(dim=1024), nullable=True)
    image_embedding = mapped_column(Vector(dim=1024), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    report: Mapped["FAReport"] = relationship(back_populates="cases")

    __table_args__ = (
        Index(
            "idx_cases_fts",
            text(
                "to_tsvector('simple', "
                "coalesce(customer,'') || ' ' || "
                "coalesce(device,'') || ' ' || "
                "coalesce(model,'') || ' ' || "
                "coalesce(defect_mode,'') || ' ' || "
                "coalesce(fa_status,'') || ' ' || "
                "coalesce(follow_up,''))"
            ),
            postgresql_using="gin",
        ),
    )
