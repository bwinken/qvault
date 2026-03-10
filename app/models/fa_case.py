import datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class FAReport(Base):
    __tablename__ = "fa_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str]
    upload_date: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    report_date: Mapped[datetime.date | None] = mapped_column(Date)
    total_slides: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="processing")  # processing / review / done / error
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    cases: Mapped[list["FACase"]] = relationship(back_populates="report", cascade="all, delete-orphan")


class FACase(Base):
    __tablename__ = "fa_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("fa_reports.id", ondelete="CASCADE"))
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
