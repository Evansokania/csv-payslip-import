from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, func
from sqlalchemy.dialects.mysql import BIGINT as MySQLBigInteger
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.schema import UniqueConstraint


class Base(DeclarativeBase):
    pass


class PayrollImportRun(Base):
    __tablename__ = "payroll_import_runs"
    __table_args__ = (
        Index("payroll_import_runs_payroll_month_status_index", "payroll_month", "status"),
        Index("payroll_import_runs_csv_sha256_index", "csv_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payroll_month: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(64), default="created")
    csv_sha256: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    format_version: Mapped[str] = mapped_column(String(32), default="v1")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    error_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    export_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    balances_applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    raw_rows: Mapped[list["PayrollImportRawRow"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    column_maps: Mapped[list["PayrollImportColumnMap"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["PayrollImportSnapshot"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class PayrollImportRawRow(Base):
    __tablename__ = "payroll_import_raw_rows"
    __table_args__ = (
        UniqueConstraint("import_run_id", "row_no", name="payroll_import_raw_rows_import_run_id_row_no_unique"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("payroll_import_runs.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["PayrollImportRun"] = relationship(back_populates="raw_rows")


class PayrollImportColumnMap(Base):
    __tablename__ = "payroll_import_column_maps"
    # Short name: MySQL max identifier length is 64 chars (matches Laravel migration).
    __table_args__ = (
        UniqueConstraint(
            "import_run_id",
            "csv_header_normalized",
            name="prl_imp_colmap_run_hdr_uq",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("payroll_import_runs.id", ondelete="CASCADE"), index=True)
    csv_header_raw: Mapped[str] = mapped_column(String(512))
    csv_header_normalized: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32))
    match_type: Mapped[str] = mapped_column(String(64), default="unresolved")
    allowance_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deduction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    display_label_override: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    loan_match_rule: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["PayrollImportRun"] = relationship(back_populates="column_maps")


class Employee(Base):
    """Minimal ORM anchor so FK payroll_import_snapshots.employee_id -> employees.id resolves."""

    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(MySQLBigInteger(unsigned=True), primary_key=True)


class PayrollImportSnapshot(Base):
    __tablename__ = "payroll_import_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "import_run_id",
            "employee_id",
            name="payroll_import_snapshots_import_run_id_employee_id_unique",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("payroll_import_runs.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int] = mapped_column(
        MySQLBigInteger(unsigned=True), ForeignKey("employees.id", ondelete="CASCADE")
    )
    earnings_lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    deduction_lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    computed: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    dimensions: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["PayrollImportRun"] = relationship(back_populates="snapshots")


class PayrollImportSynonym(Base):
    """Header text → allowance/deduction hints (optional; matches Laravel tenant migration)."""

    __tablename__ = "payroll_import_synonyms"
    __table_args__ = (
        UniqueConstraint(
            "normalized_header",
            name="payroll_import_synonyms_normalized_header_unique",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    normalized_header: Mapped[str] = mapped_column(String(512))
    allowance_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deduction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class PayrollImportClassification(Base):
    """Wage-type classification (from the tenant's classification sheet).

    Drives import column routing: header -> section (Earning / Statutory Deduction /
    Other Deduction / Memo / Calculated) so memo/calculated columns are excluded and
    earnings/deductions route correctly regardless of banner layout.
    """

    __tablename__ = "payroll_import_classifications"
    __table_args__ = (
        UniqueConstraint("normalized_header", name="prl_imp_classif_hdr_uq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    normalized_header: Mapped[str] = mapped_column(String(512))
    raw_header: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    section: Mapped[str] = mapped_column(String(64))
    nature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Allowance(Base):
    __tablename__ = "allowances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))


class Deduction(Base):
    __tablename__ = "deductions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
