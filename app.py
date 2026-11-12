import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go

import sqlite3
import os
import uuid
import hashlib
import json
import math
import io
import re
from datetime import datetime, date, timedelta, timezone
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

st.set_page_config(
    page_title="Industrial Emissions Monitoring and Reduction Planner",
    page_icon="🏭",
    layout="wide",
)

APP_NAME = "Industrial Emissions Monitoring and Reduction Planner"
ENGINE_VERSION = "0.1.1"
SCHEMA_VERSION = "1.0.1"

DB_PATH = os.path.join(os.getcwd(), "emissions_planner.db")
DATA_DIR = os.path.join(os.getcwd(), "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
SAMPLE_DIR = os.path.join(DATA_DIR, "sample")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

TODAY = datetime.now(timezone.utc).date()

ROLES = [
    "administrator",
    "analyst/operator",
    "reviewer",
    "read-only stakeholder",
]

PERMISSIONS = {
    "administrator": {
        "view", "export", "create", "import", "edit_draft", "submit",
        "approve", "reject", "archive", "override", "run_analysis",
        "manage_reference", "restore_backup"
    },
    "analyst/operator": {
        "view", "export", "create", "import", "edit_draft", "submit", "run_analysis"
    },
    "reviewer": {
        "view", "export", "approve", "reject", "override", "run_analysis"
    },
    "read-only stakeholder": {
        "view", "export", "run_analysis"
    },
}

ALLOWED_TRANSITIONS = {
    "draft": {"submitted", "exception"},
    "submitted": {"approved", "rejected", "draft", "exception"},
    "exception": {"draft", "rejected", "approved"},
    "rejected": {"draft"},
    "approved": {"archived"},
    "archived": {"draft"},
}

REQUIRED_ACTIVITY_COLS = [
    "facility_id",
    "period_start",
    "period_end",
    "scope",
    "source_category",
    "activity_type",
    "quantity",
    "unit",
    "measurement_status",
    "data_quality",
]

OPTIONAL_ACTIVITY_COLS = [
    "source_system",
    "provenance",
]

MEASUREMENT_STATUSES = {"measured", "estimated"}
DATA_QUALITIES = {"high", "medium", "low"}
SCOPES = {1, 2, 3}
MAX_QTY = 1e12
MIN_DATE = date(2000, 1, 1)

ERROR_COLUMNS = ["row", "field", "value", "severity", "issue", "suggested_fix"]

VALID_COLUMNS = [
    "id", "facility_id", "period_start", "period_end", "scope",
    "source_category", "activity_type", "quantity", "unit",
    "measurement_status", "data_quality", "source_system", "provenance",
    "status", "exception_flags", "created_at", "updated_at",
    "schema_version", "is_demo", "_row", "_key", "_existing_id"
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}{raw}" if prefix else raw


def ensure_dirs() -> None:
    for p in [DATA_DIR, UPLOAD_DIR, SAMPLE_DIR, BACKUP_DIR]:
        os.makedirs(p, exist_ok=True)


def canonical_col(name: Any) -> str:
    s = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s


def parse_date_value(value: Any) -> Optional[date]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "nat", ""}:
        return None
    try:
        dt = pd.to_datetime(s, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None


def parse_float_value(value: Any) -> Optional[float]:
    try:
        s = str(value).strip().replace(",", "")
        if not s or s.lower() in {"nan", "none", ""}:
            return None
        x = float(s)
        if not math.isfinite(x):
            return None
        return x
    except Exception:
        return None


def parse_int_value(value: Any) -> Optional[int]:
    try:
        f = parse_float_value(value)
        if f is None:
            return None
        if abs(f - round(f)) > 1e-9:
            return None
        return int(round(f))
    except Exception:
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    return default if s.lower() in {"nan", "none", "nat"} else s


def full_month_ranges(count: int) -> List[Tuple[date, date]]:
    if count <= 0:
        return []
    last_end = TODAY.replace(day=1) - timedelta(days=1)
    starts = pd.date_range(end=last_end, periods=count, freq="MS")
    out = []
    for s in starts:
        start_date = s.date()
        end_date = (s + pd.offsets.MonthEnd(1)).date()
        out.append((start_date, end_date))
    return out


def activity_natural_key(facility_id: str, period_start: str, period_end: str, scope: int, activity_type: str) -> str:
    return f"{facility_id}|{period_start}|{period_end}|{scope}|{activity_type}"


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS facilities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            sector TEXT,
            location TEXT,
            timezone TEXT DEFAULT 'UTC',
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            provenance TEXT,
            is_demo INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS activity_records (
            id TEXT PRIMARY KEY,
            facility_id TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            scope INTEGER NOT NULL,
            source_category TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT NOT NULL,
            measurement_status TEXT NOT NULL,
            data_quality TEXT NOT NULL,
            source_system TEXT,
            provenance TEXT,
            status TEXT NOT NULL,
            exception_flags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            is_demo INTEGER DEFAULT 0,
            FOREIGN KEY(facility_id) REFERENCES facilities(id)
        );

        CREATE TABLE IF NOT EXISTS emission_factors (
            id TEXT PRIMARY KEY,
            factor_set_id TEXT,
            pollutant TEXT NOT NULL DEFAULT 'CO2e',
            activity_type TEXT NOT NULL,
            unit TEXT NOT NULL,
            value REAL NOT NULL,
            value_unit TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            source_reference TEXT,
            version TEXT,
            jurisdiction TEXT DEFAULT 'global',
            created_at TEXT NOT NULL,
            is_demo INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reduction_options (
            id TEXT PRIMARY KEY,
            facility_id TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            description TEXT,
            capex REAL DEFAULT 0,
            opex_savings_annual REAL DEFAULT 0,
            implementation_months INTEGER DEFAULT 0,
            applies_scope INTEGER,
            applies_activity_type TEXT,
            activity_reduction_pct REAL DEFAULT 0,
            factor_improvement_pct REAL DEFAULT 0,
            capture_rate_pct REAL DEFAULT 0,
            expected_annual_reduction_override REAL,
            confidence REAL DEFAULT 0.5,
            status TEXT DEFAULT 'draft',
            owner TEXT,
            evidence TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            is_demo INTEGER DEFAULT 0,
            FOREIGN KEY(facility_id) REFERENCES facilities(id)
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            details TEXT
        );

        CREATE TABLE IF NOT EXISTS imports (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            checksum TEXT NOT NULL,
            rows_total INTEGER NOT NULL,
            rows_valid INTEGER NOT NULL,
            rows_invalid INTEGER NOT NULL,
            rows_updated INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            storage_path TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            import_id TEXT,
            filename TEXT NOT NULL,
            checksum TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            content_type TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reference_versions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            effective_from TEXT,
            source_reference TEXT,
            notes TEXT,
            is_demo INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_activity_fac_period
            ON activity_records(facility_id, period_end);

        CREATE INDEX IF NOT EXISTS idx_activity_status
            ON activity_records(status);

        CREATE INDEX IF NOT EXISTS idx_factor_lookup
            ON emission_factors(activity_type, unit, pollutant, effective_from);

        CREATE INDEX IF NOT EXISTS idx_options_facility
            ON reduction_options(facility_id, status);
        """
    )
    conn.commit()


def insert_row(conn: sqlite3.Connection, table: str, data: Dict[str, Any]) -> None:
    cols = list(data.keys())
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})"
    conn.execute(sql, tuple(data.values()))


def seed_reference_data(conn: sqlite3.Connection) -> None:
    rows = [
        {
            "id": "REF-CATALOG-2026",
            "name": "Synthetic emission factor catalog",
            "version": "2026.1-synthetic",
            "effective_from": (TODAY - timedelta(days=180)).isoformat(),
            "source_reference": "Synthetic demonstration catalog; not for regulatory use",
            "notes": "Used only for local software demonstration and test plumbing.",
            "is_demo": 1,
        },
        {
            "id": "REF-SCHEMA-1",
            "name": "Activity import schema",
            "version": SCHEMA_VERSION,
            "effective_from": TODAY.isoformat(),
            "source_reference": "Application schema",
            "notes": "Versioned import and calculation schema.",
            "is_demo": 1,
        },
    ]
    for r in rows:
        insert_row(conn, "reference_versions", r)


def seed_facilities(conn: sqlite3.Connection) -> None:
    ts = now_iso()
    facilities = [
        ("FAC-STL-001", "Steelworks North", "Primary steel", "Synthetic City North", "Europe/Berlin"),
        ("FAC-CHM-002", "Chemical Park South", "Specialty chemicals", "Synthetic City South", "Europe/Paris"),
        ("FAC-CMT-003", "Cement Works East", "Cement and clinker", "Synthetic City East", "Europe/Madrid"),
    ]
    for fid, name, sector, loc, tz in facilities:
        insert_row(
            conn,
            "facilities",
            {
                "id": fid,
                "name": name,
                "sector": sector,
                "location": loc,
                "timezone": tz,
                "status": "active",
                "created_at": ts,
                "updated_at": ts,
                "schema_version": SCHEMA_VERSION,
                "provenance": "SYNTHETIC_DEMO",
                "is_demo": 1,
            },
        )


def seed_factors(conn: sqlite3.Connection) -> None:
    ts = now_iso()

    def years_ago(n: float) -> date:
        return TODAY - timedelta(days=int(365.25 * n))

    def days_ago(n: int) -> date:
        return TODAY - timedelta(days=n)

    static_factors = [
        ("FACT-NG-STD", "natural_gas", "MWh", 202.0, "kg CO2e/MWh", "SYNTH-NG-2026"),
        ("FACT-DIESEL-STD", "diesel", "litres", 2.68, "kg CO2e/litre", "SYNTH-DIESEL-2026"),
        ("FACT-LPG-STD", "lpg_combustion", "litres", 1.52, "kg CO2e/litre", "SYNTH-LPG-2026"),
        ("FACT-CLINKER-STD", "process_calcination", "tonnes_clinker", 0.52, "tCO2e/tonne_clinker", "SYNTH-CEM-2026"),
        ("FACT-WW-STD", "wastewater_treatment", "m3", 0.06, "kg CO2e/m3", "SYNTH-WW-2026"),
        ("FACT-FGAS-STD", "refrigerant_leak", "kg_refrigerant", 2.088, "tCO2e/kg_refrigerant", "SYNTH-FGAS-2026"),
    ]

    for fid, activity, unit, value, value_unit, source in static_factors:
        insert_row(
            conn,
            "emission_factors",
            {
                "id": fid,
                "factor_set_id": "REF-CATALOG-2026",
                "pollutant": "CO2e",
                "activity_type": activity,
                "unit": unit,
                "value": value,
                "value_unit": value_unit,
                "effective_from": years_ago(5).isoformat(),
                "effective_to": None,
                "source_reference": source,
                "version": "synthetic-1",
                "jurisdiction": "global",
                "created_at": ts,
                "is_demo": 1,
            },
        )

    grid_versions = [
        ("FACT-GRID-V1", years_ago(3), years_ago(2) - timedelta(days=1), 420.0),
        ("FACT-GRID-V2", years_ago(2), years_ago(1) - timedelta(days=1), 385.0),
        ("FACT-GRID-V3", years_ago(1), days_ago(180) - timedelta(days=1), 350.0),
        ("FACT-GRID-V4", days_ago(180), None, 330.0),
    ]

    for fid, eff_from, eff_to, value in grid_versions:
        insert_row(
            conn,
            "emission_factors",
            {
                "id": fid,
                "factor_set_id": "REF-CATALOG-2026",
                "pollutant": "CO2e",
                "activity_type": "grid_electricity",
                "unit": "MWh",
                "value": value,
                "value_unit": "kg CO2e/MWh",
                "effective_from": eff_from.isoformat(),
                "effective_to": eff_to.isoformat() if eff_to else None,
                "source_reference": "SYNTH-GRID-TIME-SERIES",
                "version": f"synthetic-{fid}",
                "jurisdiction": "regional-demo",
                "created_at": ts,
                "is_demo": 1,
            },
        )


def seed_activities(conn: sqlite3.Connection) -> None:
    ts = now_iso()
    rng = np.random.default_rng(120)
    months = full_month_ranges(18)

    source_templates = [
        dict(facility_id="FAC-STL-001", scope=1, source_category="Stationary combustion", activity_type="natural_gas", unit="MWh", low=9000, high=16000, system="SCADA_MES"),
        dict(facility_id="FAC-STL-001", scope=1, source_category="Stationary combustion", activity_type="diesel", unit="litres", low=60000, high=130000, system="FUEL_LOG"),
        dict(facility_id="FAC-STL-001", scope=2, source_category="Purchased electricity", activity_type="grid_electricity", unit="MWh", low=8000, high=15000, system="UTILITY_BILL"),
        dict(facility_id="FAC-STL-001", scope=1, source_category="Fugitive emissions", activity_type="refrigerant_leak", unit="kg_refrigerant", low=15, high=70, system="MAINTENANCE_LOG"),

        dict(facility_id="FAC-CHM-002", scope=1, source_category="Stationary combustion", activity_type="natural_gas", unit="MWh", low=18000, high=31000, system="SCADA_MES"),
        dict(facility_id="FAC-CHM-002", scope=2, source_category="Purchased electricity", activity_type="grid_electricity", unit="MWh", low=16000, high=26000, system="UTILITY_BILL"),
        dict(facility_id="FAC-CHM-002", scope=3, source_category="Wastewater", activity_type="wastewater_treatment", unit="m3", low=4500, high=9500, system="WATER_METER"),

        dict(facility_id="FAC-CMT-003", scope=1, source_category="Process emissions", activity_type="process_calcination", unit="tonnes_clinker", low=38000, high=66000, system="PRODUCTION_MES"),
        dict(facility_id="FAC-CMT-003", scope=1, source_category="Stationary combustion", activity_type="natural_gas", unit="MWh", low=14000, high=23000, system="SCADA_MES"),
        dict(facility_id="FAC-CMT-003", scope=2, source_category="Purchased electricity", activity_type="grid_electricity", unit="MWh", low=11000, high=19000, system="UTILITY_BILL"),
    ]

    for mi, (start, end) in enumerate(months):
        is_old = mi < len(months) - 4
        is_mid = len(months) - 4 <= mi < len(months) - 1
        status = "approved" if is_old else ("submitted" if is_mid else "draft")
        measurement = "measured" if mi < len(months) - 3 else "estimated"
        dq = "high" if mi < len(months) - 6 else ("medium" if mi < len(months) - 2 else "low")

        for tpl in source_templates:
            seasonal = 1.0 + 0.08 * math.sin(mi / 3.0)
            qty = float(rng.uniform(tpl["low"], tpl["high"])) * seasonal
            qty = round(qty, 2)

            flags: List[str] = []
            rec_status = status
            if mi == len(months) - 1 and tpl["activity_type"] == "grid_electricity" and tpl["facility_id"] == "FAC-CMT-003":
                rec_status = "exception"
                flags = ["late_bill_expected", "estimated_pending_confirmation"]

            insert_row(
                conn,
                "activity_records",
                {
                    "id": uid("ACT-"),
                    "facility_id": tpl["facility_id"],
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "scope": tpl["scope"],
                    "source_category": tpl["source_category"],
                    "activity_type": tpl["activity_type"],
                    "quantity": qty,
                    "unit": tpl["unit"],
                    "measurement_status": measurement,
                    "data_quality": dq,
                    "source_system": tpl["system"],
                    "provenance": "SYNTHETIC_DEMO_GENERATOR",
                    "status": rec_status,
                    "exception_flags": json.dumps(flags),
                    "created_at": ts,
                    "updated_at": ts,
                    "schema_version": SCHEMA_VERSION,
                    "is_demo": 1,
                },
            )

    latest_start, latest_end = months[-1]

    insert_row(
        conn,
        "activity_records",
        {
            "id": uid("ACT-"),
            "facility_id": "FAC-CHM-002",
            "period_start": latest_start.isoformat(),
            "period_end": latest_end.isoformat(),
            "scope": 1,
            "source_category": "Fugitive emissions",
            "activity_type": "hydrogen_leak",
            "quantity": 120.0,
            "unit": "kg_hydrogen",
            "measurement_status": "estimated",
            "data_quality": "low",
            "source_system": "FIELD_NOTE",
            "provenance": "SYNTHETIC_DEMO_EXCEPTION",
            "status": "exception",
            "exception_flags": json.dumps(["missing_emission_factor"]),
            "created_at": ts,
            "updated_at": ts,
            "schema_version": SCHEMA_VERSION,
            "is_demo": 1,
        },
    )

    insert_row(
        conn,
        "activity_records",
        {
            "id": uid("ACT-"),
            "facility_id": "FAC-STL-001",
            "period_start": latest_start.isoformat(),
            "period_end": latest_end.isoformat(),
            "scope": 1,
            "source_category": "Stationary combustion",
            "activity_type": "lpg_combustion",
            "quantity": 5400.0,
            "unit": "litres",
            "measurement_status": "measured",
            "data_quality": "low",
            "source_system": "MANUAL_METER",
            "provenance": "SYNTHETIC_DEMO_EXCEPTION",
            "status": "exception",
            "exception_flags": json.dumps(["contradictory_measurement_quality"]),
            "created_at": ts,
            "updated_at": ts,
            "schema_version": SCHEMA_VERSION,
            "is_demo": 1,
        },
    )


def seed_options(conn: sqlite3.Connection) -> None:
    ts = now_iso()
    options = [
        {
            "id": "OPT-STL-HEAT-RECOVERY",
            "facility_id": "FAC-STL-001",
            "name": "Furnace heat recovery",
            "category": "Energy efficiency",
            "description": "Recover waste heat from furnace exhaust for preheating.",
            "capex": 420000.0,
            "opex_savings_annual": 115000.0,
            "implementation_months": 14,
            "applies_scope": 1,
            "applies_activity_type": "natural_gas",
            "activity_reduction_pct": 12.0,
            "factor_improvement_pct": 0.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.82,
            "status": "submitted",
            "owner": "Plant energy team",
            "evidence": "Synthetic engineering estimate",
        },
        {
            "id": "OPT-STL-VSD",
            "facility_id": "FAC-STL-001",
            "name": "Variable speed drives and LED lighting",
            "category": "Electricity efficiency",
            "description": "Reduce electricity demand from motors and lighting.",
            "capex": 160000.0,
            "opex_savings_annual": 52000.0,
            "implementation_months": 8,
            "applies_scope": 2,
            "applies_activity_type": "grid_electricity",
            "activity_reduction_pct": 9.0,
            "factor_improvement_pct": 0.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.88,
            "status": "approved",
            "owner": "Facilities engineering",
            "evidence": "Synthetic retro-fit study",
        },
        {
            "id": "OPT-STL-LEAK",
            "facility_id": "FAC-STL-001",
            "name": "Refrigerant leak detection and repair",
            "category": "Fugitive emissions control",
            "description": "Continuous monitoring and faster repair of refrigerant leaks.",
            "capex": 90000.0,
            "opex_savings_annual": 28000.0,
            "implementation_months": 5,
            "applies_scope": 1,
            "applies_activity_type": "refrigerant_leak",
            "activity_reduction_pct": 35.0,
            "factor_improvement_pct": 0.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.75,
            "status": "submitted",
            "owner": "Maintenance",
            "evidence": "Synthetic PDMS benchmark",
        },
        {
            "id": "OPT-CHM-PV",
            "facility_id": "FAC-CHM-002",
            "name": "On-site solar PV",
            "category": "Renewable electricity",
            "description": "Displace grid electricity with on-site solar generation.",
            "capex": 700000.0,
            "opex_savings_annual": 135000.0,
            "implementation_months": 12,
            "applies_scope": 2,
            "applies_activity_type": "grid_electricity",
            "activity_reduction_pct": 20.0,
            "factor_improvement_pct": 0.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.78,
            "status": "draft",
            "owner": "Sustainability team",
            "evidence": "Synthetic feasibility screen",
        },
        {
            "id": "OPT-CHM-WATER",
            "facility_id": "FAC-CHM-002",
            "name": "Water recycling loop",
            "category": "Material and water efficiency",
            "description": "Reduce wastewater volume through process water recycling.",
            "capex": 260000.0,
            "opex_savings_annual": 61000.0,
            "implementation_months": 16,
            "applies_scope": 3,
            "applies_activity_type": "wastewater_treatment",
            "activity_reduction_pct": 18.0,
            "factor_improvement_pct": 0.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.70,
            "status": "submitted",
            "owner": "Environmental operations",
            "evidence": "Synthetic water balance",
        },
        {
            "id": "OPT-CMT-FUEL-SWITCH",
            "facility_id": "FAC-CMT-003",
            "name": "Biomass fuel blend",
            "category": "Fuel switching",
            "description": "Blend sustainable biomass into thermal fuel supply.",
            "capex": 950000.0,
            "opex_savings_annual": -25000.0,
            "implementation_months": 24,
            "applies_scope": 1,
            "applies_activity_type": "natural_gas",
            "activity_reduction_pct": 0.0,
            "factor_improvement_pct": 28.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.62,
            "status": "draft",
            "owner": "Energy procurement",
            "evidence": "Synthetic supplier declaration",
        },
        {
            "id": "OPT-CMT-CCN-EFFICIENCY",
            "facility_id": "FAC-CMT-003",
            "name": "Calcination process control optimisation",
            "category": "Process optimisation",
            "description": "Improve kiln control stability to reduce process emissions per tonne clinker.",
            "capex": 310000.0,
            "opex_savings_annual": 72000.0,
            "implementation_months": 11,
            "applies_scope": 1,
            "applies_activity_type": "process_calcination",
            "activity_reduction_pct": 0.0,
            "factor_improvement_pct": 8.0,
            "capture_rate_pct": 0.0,
            "expected_annual_reduction_override": None,
            "confidence": 0.74,
            "status": "submitted",
            "owner": "Process engineering",
            "evidence": "Synthetic control trial",
        },
    ]

    for o in options:
        o.update(
            {
                "created_at": ts,
                "updated_at": ts,
                "schema_version": SCHEMA_VERSION,
                "is_demo": 1,
            }
        )
        insert_row(conn, "reduction_options", o)


def seed_audit(conn: sqlite3.Connection) -> None:
    insert_row(
        conn,
        "audit_events",
        {
            "id": uid("AUD-"),
            "ts": now_iso(),
            "actor_role": "system",
            "action": "demo_database_seeded",
            "entity_type": "database",
            "entity_id": DB_PATH,
            "details": json.dumps({"synthetic": True, "schema_version": SCHEMA_VERSION}),
        },
    )


def write_sample_files(conn: sqlite3.Connection) -> None:
    ensure_dirs()
    last_month = full_month_ranges(1)[0]
    start, end = last_month[0].isoformat(), last_month[1].isoformat()
    future = (TODAY + timedelta(days=10)).isoformat()

    header = (
        "facility_id,period_start,period_end,scope,source_category,activity_type,"
        "quantity,unit,measurement_status,data_quality,source_system,provenance"
    )

    valid_rows = [
        f"FAC-STL-001,{start},{end},1,Stationary combustion,lpg_combustion,1200,litres,measured,high,IMPORT_TEST,Synthetic purchase record",
        f"FAC-CHM-002,{start},{end},1,Stationary combustion,lpg_combustion,950,litres,measured,high,IMPORT_TEST,Synthetic purchase record",
    ]

    invalid_rows = [
        f"FAC-UNKNOWN,{start},{end},1,Stationary combustion,lpg_combustion,100,litres,measured,high,IMPORT_TEST,Bad facility",
        f"FAC-STL-001,{start},{end},1,Stationary combustion,lpg_combustion,-50,litres,measured,high,IMPORT_TEST,Negative quantity",
        f"FAC-STL-001,31/02/2026,{end},1,Stationary combustion,lpg_combustion,100,litres,measured,high,IMPORT_TEST,Bad date",
        f"FAC-STL-001,{start},{end},1,Stationary combustion,lpg_combustion,1200,litres,measured,high,IMPORT_TEST,Duplicate within file",
        f"FAC-CMT-003,{start},{end},1,Stationary combustion,lpg_combustion,300,,measured,high,IMPORT_TEST,Missing unit",
        f"FAC-STL-001,{future},{future},1,Stationary combustion,lpg_combustion,300,litres,measured,high,IMPORT_TEST,Future dated",
    ]

    valid_path = os.path.join(SAMPLE_DIR, "valid_activity_sample.csv")
    invalid_path = os.path.join(SAMPLE_DIR, "invalid_activity_sample.csv")

    with open(valid_path, "w", encoding="utf-8", newline="") as f:
        f.write(header + "\n")
        f.write("\n".join(valid_rows) + "\n")

    with open(invalid_path, "w", encoding="utf-8", newline="") as f:
        f.write(header + "\n")
        f.write("\n".join(invalid_rows) + "\n")


@st.cache_resource(show_spinner=False)
def bootstrap_demo() -> bool:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM facilities WHERE is_demo=1").fetchone()[0]
        if count == 0:
            seed_reference_data(conn)
            seed_facilities(conn)
            seed_factors(conn)
            seed_activities(conn)
            seed_options(conn)
            seed_audit(conn)
            conn.commit()
        write_sample_files(conn)
        conn.commit()
    finally:
        conn.close()
    return True


def get_conn() -> sqlite3.Connection:
    bootstrap_demo()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetch_df(conn: sqlite3.Connection, query: str, params: Tuple = ()) -> pd.DataFrame:
    cur = conn.execute(query, params)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def can(role: str, action: str) -> bool:
    return action in PERMISSIONS.get(role, set())


def audit(conn: sqlite3.Connection, role: str, action: str, entity_type: str, entity_id: str, details: Any = "") -> None:
    if isinstance(details, (dict, list)):
        details_str = json.dumps(details)
    else:
        details_str = str(details)
    insert_row(
        conn,
        "audit_events",
        {
            "id": uid("AUD-"),
            "ts": now_iso(),
            "actor_role": role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": details_str,
        },
    )


def get_known_activity_units(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT activity_type, unit FROM emission_factors WHERE pollutant='CO2e'"
    ).fetchall()
    known: Dict[str, Dict[str, str]] = {}
    for r in rows:
        at = safe_str(r["activity_type"]).lower()
        u = safe_str(r["unit"])
        if at and u:
            known.setdefault(at, {})[u.lower()] = u
    return known


def factor_exists(conn: sqlite3.Connection, activity_type: str, unit: str, period_end: date) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM emission_factors
        WHERE activity_type=? AND unit=? AND pollutant='CO2e'
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to >= ?)
        LIMIT 1
        """,
        (activity_type, unit, period_end.isoformat(), period_end.isoformat()),
    ).fetchone()
    return row is not None


def select_factor(conn: sqlite3.Connection, activity_type: str, unit: str, period_end: Any) -> Optional[Dict[str, Any]]:
    pe = period_end.isoformat() if isinstance(period_end, date) else safe_str(period_end)
    row = conn.execute(
        """
        SELECT *
        FROM emission_factors
        WHERE activity_type=? AND unit=? AND pollutant='CO2e'
          AND effective_from <= ?
          AND (effective_to IS NULL OR effective_to >= ?)
        ORDER BY effective_from DESC
        LIMIT 1
        """,
        (activity_type, unit, pe, pe),
    ).fetchone()
    return dict(row) if row else None


def emissions_from_quantity(quantity: float, unit: str, factor_value: float, factor_unit: str) -> float:
    fu = safe_str(factor_unit).lower()
    if fu.startswith("kg"):
        return float(quantity) * float(factor_value) / 1000.0
    if fu.startswith("t"):
        return float(quantity) * float(factor_value)
    raise ValueError(f"Unsupported factor unit: {factor_unit}")


def uncertainty_percent(rec: Dict[str, Any], factor: Dict[str, Any], period_end: date) -> float:
    base = 3.0
    measurement_penalty = 0.0 if safe_str(rec.get("measurement_status")).lower() == "measured" else 8.0
    dq_map = {"high": 0.0, "medium": 3.0, "low": 8.0}
    dq_penalty = dq_map.get(safe_str(rec.get("data_quality")).lower(), 5.0)

    eff_from = parse_date_value(factor.get("effective_from"))
    age_days = 0
    if eff_from:
        age_days = max(0, (period_end - eff_from).days)
    age_penalty = min(10.0, age_days / 90.0)

    return min(45.0, base + measurement_penalty + dq_penalty + age_penalty)


def calculate_record_emissions(conn: sqlite3.Connection, rec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    flags: List[str] = []
    activity_type = safe_str(rec.get("activity_type")).lower()
    unit = safe_str(rec.get("unit"))
    period_end = parse_date_value(rec.get("period_end"))

    if not activity_type or not unit or not period_end:
        return None, ["invalid_record_fields"]

    factor = select_factor(conn, activity_type, unit, period_end)
    if not factor:
        return None, ["missing_emission_factor"]

    if safe_str(factor.get("unit")) != unit:
        return None, ["unit_mismatch"]

    qty = safe_float(rec.get("quantity"), 0.0)
    factor_value = safe_float(factor.get("value"), 0.0)
    factor_unit = safe_str(factor.get("value_unit"))

    try:
        tco2e = emissions_from_quantity(qty, unit, factor_value, factor_unit)
    except Exception as exc:
        return None, [f"calculation_error: {exc}"]

    if factor_unit.lower().startswith("kg"):
        formula = f"{qty:.3f} {unit} × {factor_value:.3f} {factor_unit} ÷ 1000 = {tco2e:.3f} tCO2e"
    else:
        formula = f"{qty:.3f} {unit} × {factor_value:.3f} {factor_unit} = {tco2e:.3f} tCO2e"

    unc = uncertainty_percent(rec, factor, period_end)

    result = {
        "tco2e": float(tco2e),
        "uncertainty_pct": float(unc),
        "factor_id": safe_str(factor.get("id")),
        "factor_value": float(factor_value),
        "factor_unit": factor_unit,
        "factor_source": safe_str(factor.get("source_reference")),
        "factor_version": safe_str(factor.get("version")),
        "factor_effective_from": safe_str(factor.get("effective_from")),
        "factor_effective_to": safe_str(factor.get("effective_to")),
        "formula": formula,
    }
    return result, flags


def build_baseline_df(
    conn: sqlite3.Connection,
    facility_ids: Optional[List[str]] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    statuses: Tuple[str, ...] = ("approved",),
    include_exceptions: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if start is None:
        start = TODAY - timedelta(days=365)
    if end is None:
        end = TODAY

    start_s = start.isoformat() if isinstance(start, date) else safe_str(start)
    end_s = end.isoformat() if isinstance(end, date) else safe_str(end)

    status_list = list(statuses)
    if include_exceptions and "exception" not in status_list:
        status_list.append("exception")

    q = """
        SELECT a.*, f.name AS facility_name
        FROM activity_records a
        JOIN facilities f ON f.id = a.facility_id
        WHERE a.period_end >= ? AND a.period_end <= ?
    """
    params: List[Any] = [start_s, end_s]

    if facility_ids:
        q += f" AND a.facility_id IN ({', '.join(['?'] * len(facility_ids))})"
        params.extend(facility_ids)

    q += f" AND a.status IN ({', '.join(['?'] * len(status_list))})"
    params.extend(status_list)

    cur = conn.execute(q, tuple(params))
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()

    detail_rows: List[Dict[str, Any]] = []
    exception_rows: List[Dict[str, Any]] = []

    for raw in rows:
        rec = dict(zip(cols, raw))
        res, flags = calculate_record_emissions(conn, rec)
        if res is None:
            exception_rows.append(
                {
                    "record_id": rec.get("id"),
                    "facility_id": rec.get("facility_id"),
                    "facility_name": rec.get("facility_name"),
                    "period_end": rec.get("period_end"),
                    "activity_type": rec.get("activity_type"),
                    "quantity": rec.get("quantity"),
                    "unit": rec.get("unit"),
                    "status": rec.get("status"),
                    "reason": ", ".join(flags),
                }
            )
            continue

        if flags:
            exception_rows.append(
                {
                    "record_id": rec.get("id"),
                    "facility_id": rec.get("facility_id"),
                    "facility_name": rec.get("facility_name"),
                    "period_end": rec.get("period_end"),
                    "activity_type": rec.get("activity_type"),
                    "quantity": rec.get("quantity"),
                    "unit": rec.get("unit"),
                    "status": rec.get("status"),
                    "reason": ", ".join(flags),
                }
            )

        detail_rows.append({**rec, **res})

    detail_df = pd.DataFrame(detail_rows)
    exceptions_df = pd.DataFrame(exception_rows)

    if detail_df.empty:
        return pd.DataFrame(), detail_df, exceptions_df

    detail_df["period_start"] = pd.to_datetime(detail_df["period_start"])
    detail_df["period_end"] = pd.to_datetime(detail_df["period_end"])
    detail_df["tco2e_low"] = detail_df["tco2e"] * (1 - detail_df["uncertainty_pct"] / 100.0)
    detail_df["tco2e_high"] = detail_df["tco2e"] * (1 + detail_df["uncertainty_pct"] / 100.0)

    agg = (
        detail_df.groupby(
            ["facility_id", "facility_name", "scope", "source_category", "activity_type", "unit"],
            as_index=False,
        )
        .agg(
            periods=("id", "count"),
            months_covered=("period_start", lambda s: max(1, s.dt.to_period("M").nunique())),
            quantity_total=("quantity", "sum"),
            tco2e_total=("tco2e", "sum"),
            tco2e_total_low=("tco2e_low", "sum"),
            tco2e_total_high=("tco2e_high", "sum"),
            factor_ids=("factor_id", lambda x: ", ".join(sorted(set(safe_str(v) for v in x)))),
            factor_sources=("factor_source", lambda x: ", ".join(sorted(set(safe_str(v) for v in x)))),
            avg_uncertainty=("uncertainty_pct", "mean"),
            measurement_statuses=("measurement_status", lambda x: ", ".join(sorted(set(safe_str(v).lower() for v in x)))),
            data_qualities=("data_quality", lambda x: ", ".join(sorted(set(safe_str(v).lower() for v in x)))),
        )
    )

    agg["tco2e_annual"] = agg["tco2e_total"] * 12.0 / agg["months_covered"]
    agg["tco2e_annual_low"] = agg["tco2e_total_low"] * 12.0 / agg["months_covered"]
    agg["tco2e_annual_high"] = agg["tco2e_total_high"] * 12.0 / agg["months_covered"]
    agg = agg.sort_values("tco2e_annual", ascending=False).reset_index(drop=True)

    return agg, detail_df, exceptions_df


def load_options(conn: sqlite3.Connection, facility_ids: Optional[List[str]] = None) -> pd.DataFrame:
    q = "SELECT * FROM reduction_options WHERE status != 'archived'"
    params: List[Any] = []
    if facility_ids:
        q += f" AND facility_id IN ({', '.join(['?'] * len(facility_ids))})"
        params.extend(facility_ids)
    q += " ORDER BY facility_id, name"
    return fetch_df(conn, q, tuple(params))


def evaluate_options(options_df: pd.DataFrame, baseline_df: pd.DataFrame, use_overrides: bool = False) -> pd.DataFrame:
    if options_df.empty:
        return pd.DataFrame()

    out = []
    for _, o in options_df.iterrows():
        elig = pd.DataFrame()
        if not baseline_df.empty:
            elig = baseline_df[baseline_df["facility_id"] == safe_str(o["facility_id"])]

            applies_scope = o.get("applies_scope")
            if applies_scope is not None and not (isinstance(applies_scope, float) and math.isnan(applies_scope)):
                elig = elig[elig["scope"] == int(safe_float(applies_scope, 0))]

            applies_activity = safe_str(o.get("applies_activity_type"))
            if applies_activity:
                elig = elig[elig["activity_type"] == applies_activity.lower()]

        baseline_annual = float(elig["tco2e_annual"].sum()) if not elig.empty else 0.0

        ar = safe_float(o.get("activity_reduction_pct"), 0.0) / 100.0
        fi = safe_float(o.get("factor_improvement_pct"), 0.0) / 100.0
        cr = safe_float(o.get("capture_rate_pct"), 0.0) / 100.0

        post = baseline_annual * (1 - ar) * (1 - fi) * (1 - cr)
        calculated_reduction = max(0.0, baseline_annual - post)

        override = o.get("expected_annual_reduction_override")
        warnings: List[str] = []
        if use_overrides and override is not None and not (isinstance(override, float) and math.isnan(override)):
            reduction = max(0.0, safe_float(override, 0.0))
            if reduction > baseline_annual + 1e-6:
                reduction = baseline_annual
                warnings.append("override_capped_to_eligible_baseline")
        else:
            reduction = calculated_reduction

        capex = safe_float(o.get("capex"), 0.0)
        opex = safe_float(o.get("opex_savings_annual"), 0.0)
        confidence = safe_float(o.get("confidence"), 0.5)

        cost_per_t = capex / reduction if reduction > 0 else float("inf")
        payback = capex / opex if opex > 0 else None

        elig_unc = float(elig["avg_uncertainty"].mean()) if not elig.empty and "avg_uncertainty" in elig else 10.0
        unc_pct = min(50.0, (1.0 - confidence) * 25.0 + elig_unc * 0.3)
        red_low = max(0.0, reduction * (1 - unc_pct / 100.0))
        red_high = reduction * (1 + unc_pct / 100.0)

        explanation = (
            f"Eligible annual baseline: {baseline_annual:,.1f} tCO2e. "
            f"Activity reduction: {ar*100:.0f}%. Factor improvement: {fi*100:.0f}%. "
            f"Capture: {cr*100:.0f}%. Confidence: {confidence:.2f}."
        )

        row = o.to_dict()
        row.update(
            {
                "eligible_baseline_annual": baseline_annual,
                "calculated_reduction_annual": calculated_reduction,
                "reduction_annual": reduction,
                "reduction_low": red_low,
                "reduction_high": red_high,
                "post_reduction_annual": max(0.0, baseline_annual - reduction),
                "cost_per_tco2e": cost_per_t,
                "payback_years": payback,
                "uncertainty_pct": unc_pct,
                "option_warnings": "; ".join(warnings),
                "explanation": explanation,
                "eligible_groups": len(elig),
            }
        )
        out.append(row)

    return pd.DataFrame(out)


def optimize_portfolio(
    evaluated_df: pd.DataFrame,
    budget: float,
    max_months: int,
    min_confidence: float,
) -> Tuple[List[str], float, float, float, pd.DataFrame]:
    if evaluated_df.empty or budget < 0:
        return [], 0.0, 0.0, 0.0, evaluated_df.copy()

    feas = evaluated_df[
        (evaluated_df["capex"].apply(safe_float) <= budget)
        & (evaluated_df["implementation_months"].apply(lambda x: int(safe_float(x, 0))) <= max_months)
        & (evaluated_df["confidence"].apply(lambda x: safe_float(x, 0.0)) >= min_confidence)
        & (evaluated_df["reduction_annual"].apply(safe_float) > 0)
        & (evaluated_df["status"] != "archived")
    ].copy()

    feas = feas.sort_values(["cost_per_tco2e", "reduction_annual"], ascending=[True, False])

    selected: List[str] = []
    spent = 0.0
    reduction = 0.0
    opex = 0.0

    for _, o in feas.iterrows():
        capex = safe_float(o["capex"], 0.0)
        if spent + capex <= budget:
            selected.append(safe_str(o["id"]))
            spent += capex
            reduction += safe_float(o["reduction_annual"], 0.0)
            opex += safe_float(o["opex_savings_annual"], 0.0)

    return selected, spent, reduction, opex, feas


def monte_carlo_baseline(baseline_df: pd.DataFrame, iterations: int = 300, seed: int = 12) -> np.ndarray:
    if baseline_df.empty or iterations <= 0:
        return np.array([0.0])

    rng = np.random.default_rng(seed)
    totals = np.zeros(iterations)

    for _, r in baseline_df.iterrows():
        mid = safe_float(r.get("tco2e_annual"), 0.0)
        low = max(0.0, safe_float(r.get("tco2e_annual_low"), mid))
        high = max(low, safe_float(r.get("tco2e_annual_high"), mid))
        mid = min(max(mid, low), high)

        if high - low < 1e-9:
            samples = np.full(iterations, mid)
        else:
            samples = rng.triangular(low, mid, high, iterations)
        totals += samples

    return totals


def simulate_post_samples(baseline_samples: np.ndarray, reduction_total: float, baseline_mean: Optional[float] = None) -> np.ndarray:
    if baseline_samples.size == 0:
        return np.array([0.0])
    if baseline_mean is None:
        baseline_mean = float(np.mean(baseline_samples))
    if baseline_mean <= 0:
        return baseline_samples * 0.0
    frac = min(0.95, max(0.0, safe_float(reduction_total, 0.0) / baseline_mean))
    return baseline_samples * (1.0 - frac)


def percentile_stats(arr: np.ndarray) -> Dict[str, float]:
    if arr.size == 0:
        return {"p10": 0.0, "p50": 0.0, "p90": 0.0, "mean": 0.0}
    return {
        "p10": float(np.percentile(arr, 10)),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "mean": float(np.mean(arr)),
    }


def read_tabular_bytes(filename: str, data: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    if lower.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data), dtype=str, sheet_name=0, keep_default_na=False)
    raise ValueError("Unsupported file type. Use CSV or XLSX.")


def store_original_file(filename: str, data: bytes, checksum: str) -> str:
    ensure_dirs()
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = os.path.join(UPLOAD_DIR, f"{stamp}_{checksum[:8]}_{safe_name}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def validate_activity_dataframe(
    raw_df: pd.DataFrame,
    conn: sqlite3.Connection,
    allow_update: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    raw_df = raw_df.copy()
    raw_df.columns = [canonical_col(c) for c in raw_df.columns]

    errors: List[Dict[str, Any]] = []
    total_rows = len(raw_df)

    if total_rows == 0:
        errors.append(
            {
                "row": 0,
                "field": "",
                "value": "",
                "severity": "error",
                "issue": "empty_file",
                "suggested_fix": "Upload a CSV or XLSX file with at least one data row.",
            }
        )
        return pd.DataFrame(columns=VALID_COLUMNS), pd.DataFrame(errors, columns=ERROR_COLUMNS), {
            "total_rows": 0,
            "valid_rows": 0,
            "error_rows": 0,
            "warning_rows": 0,
        }

    missing_cols = [c for c in REQUIRED_ACTIVITY_COLS if c not in raw_df.columns]
    if missing_cols:
        for c in missing_cols:
            errors.append(
                {
                    "row": 0,
                    "field": c,
                    "value": "",
                    "severity": "error",
                    "issue": "missing_required_column",
                    "suggested_fix": f"Add required column: {c}",
                }
            )
        return pd.DataFrame(columns=VALID_COLUMNS), pd.DataFrame(errors, columns=ERROR_COLUMNS), {
            "total_rows": total_rows,
            "valid_rows": 0,
            "error_rows": total_rows,
            "warning_rows": 0,
        }

    facility_ids = {safe_str(r["id"]) for r in conn.execute("SELECT id FROM facilities").fetchall()}
    known_units = get_known_activity_units(conn)

    existing_rows: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT id, facility_id, period_start, period_end, scope, activity_type, status FROM activity_records"
    ).fetchall():
        d = dict(r)
        key = activity_natural_key(
            safe_str(d["facility_id"]),
            safe_str(d["period_start"]),
            safe_str(d["period_end"]),
            int(safe_float(d["scope"], 0)),
            safe_str(d["activity_type"]).lower(),
        )
        existing_rows[key] = d

    valid_rows: List[Dict[str, Any]] = []
    seen_keys: Dict[str, int] = {}

    for idx, row in raw_df.iterrows():
        row_num = int(idx) + 1
        row_errors: List[Dict[str, Any]] = []
        row_warnings: List[Dict[str, Any]] = []

        def add_issue(severity: str, field: str, issue: str, fix: str, value: Any = "") -> None:
            item = {
                "row": row_num,
                "field": field,
                "value": safe_str(value)[:120],
                "severity": severity,
                "issue": issue,
                "suggested_fix": fix,
            }
            if severity == "error":
                row_errors.append(item)
            else:
                row_warnings.append(item)

        facility_id = safe_str(row.get("facility_id"))
        period_start_raw = row.get("period_start")
        period_end_raw = row.get("period_end")
        scope_raw = row.get("scope")
        source_category = safe_str(row.get("source_category"))
        activity_type = safe_str(row.get("activity_type")).lower()
        quantity_raw = row.get("quantity")
        unit_raw = safe_str(row.get("unit"))
        measurement_status = safe_str(row.get("measurement_status")).lower()
        data_quality = safe_str(row.get("data_quality")).lower()
        source_system = safe_str(row.get("source_system"))
        provenance = safe_str(row.get("provenance"))

        if not facility_id:
            add_issue("error", "facility_id", "required_field_missing", "Provide an existing facility_id.")
        elif facility_id not in facility_ids:
            add_issue("error", "facility_id", "unknown_facility", "Use a facility_id that exists in the reference catalog.", facility_id)

        ps = parse_date_value(period_start_raw)
        pe = parse_date_value(period_end_raw)

        if ps is None:
            add_issue("error", "period_start", "invalid_or_missing_date", "Use ISO date format YYYY-MM-DD.", period_start_raw)
        if pe is None:
            add_issue("error", "period_end", "invalid_or_missing_date", "Use ISO date format YYYY-MM-DD.", period_end_raw)

        if ps and pe:
            if ps > pe:
                add_issue("error", "period_end", "period_start_after_period_end", "period_start must be on or before period_end.")
            if pe > TODAY + timedelta(days=1):
                add_issue("error", "period_end", "future_dated_record", "period_end cannot be in the future.", pe.isoformat())
            if ps < MIN_DATE:
                add_issue("error", "period_start", "date_out_of_range", f"period_start must be on or after {MIN_DATE.isoformat()}.", ps.isoformat())
            if pe < TODAY - timedelta(days=90):
                add_issue("warning", "period_end", "late_data", "Record is older than 90 days; confirm whether it should be entered now.", pe.isoformat())

        scope_v = parse_int_value(scope_raw)
        if scope_v is None:
            add_issue("error", "scope", "invalid_scope", "Scope must be 1, 2, or 3.", scope_raw)
        elif scope_v not in SCOPES:
            add_issue("error", "scope", "scope_out_of_range", "Scope must be 1, 2, or 3.", scope_v)

        if not source_category:
            add_issue("error", "source_category", "required_field_missing", "Provide a source category such as Stationary combustion.")

        if not activity_type:
            add_issue("error", "activity_type", "required_field_missing", "Provide an activity_type from the reference catalog.")
        elif activity_type not in known_units:
            add_issue("error", "activity_type", "activity_type_not_in_catalog", "Use an activity_type present in the emission factor catalog.", activity_type)

        unit_v = unit_raw
        if activity_type in known_units:
            unit_map = known_units[activity_type]
            if not unit_raw:
                add_issue("error", "unit", "required_field_missing", "Provide the activity unit.")
            elif unit_raw.lower() not in unit_map:
                allowed = ", ".join(sorted(unit_map.values()))
                add_issue("error", "unit", "unit_not_allowed_for_activity", f"Allowed units for {activity_type}: {allowed}.", unit_raw)
            else:
                unit_v = unit_map[unit_raw.lower()]
        elif unit_raw:
            unit_v = unit_raw

        qty_v = parse_float_value(quantity_raw)
        if qty_v is None:
            add_issue("error", "quantity", "invalid_numeric_quantity", "Quantity must be a finite number.", quantity_raw)
        elif qty_v < 0:
            add_issue("error", "quantity", "negative_quantity", "Quantity cannot be negative.", qty_v)
        elif qty_v > MAX_QTY:
            add_issue("error", "quantity", "quantity_out_of_range", f"Quantity exceeds maximum {MAX_QTY:.0e}.", qty_v)

        if measurement_status not in MEASUREMENT_STATUSES:
            add_issue("error", "measurement_status", "invalid_measurement_status", "Use measured or estimated.", measurement_status)

        if data_quality not in DATA_QUALITIES:
            add_issue("error", "data_quality", "invalid_data_quality", "Use high, medium, or low.", data_quality)

        if qty_v == 0 and measurement_status == "measured":
            add_issue("warning", "quantity", "zero_measured_activity", "Measured zero activity should be reviewed.", qty_v)

        if measurement_status == "measured" and data_quality == "low":
            add_issue("warning", "data_quality", "contradictory_measurement_quality", "Measured data with low quality should be reviewed.")

        if not source_system or not provenance:
            add_issue("warning", "provenance", "missing_provenance", "Source system and provenance improve auditability.")

        if activity_type in known_units and unit_v and pe:
            if not factor_exists(conn, activity_type, unit_v, pe):
                add_issue("warning", "activity_type", "missing_emission_factor", "No dated CO2e factor found for this activity/unit/period.")

        key = None
        existing_id = None
        if facility_id and ps and pe and scope_v is not None and activity_type:
            key = activity_natural_key(facility_id, ps.isoformat(), pe.isoformat(), scope_v, activity_type)
            if key in seen_keys:
                add_issue("error", "facility_id", "duplicate_within_file", f"Duplicate natural key first seen on row {seen_keys[key]}.", key)
            else:
                seen_keys[key] = row_num

            if key in existing_rows:
                ex = existing_rows[key]
                ex_status = safe_str(ex.get("status")).lower()
                if ex_status in {"approved", "archived", "rejected"}:
                    add_issue("error", "facility_id", "cannot_overwrite_validated_record", f"Existing record status is {ex_status}. Correct it in the review workflow instead.", key)
                elif allow_update and ex_status in {"draft", "submitted", "exception"}:
                    add_issue("warning", "facility_id", "will_update_existing_draft", "Existing draft/submitted/exception record will be updated.", key)
                    existing_id = safe_str(ex.get("id"))
                else:
                    add_issue("error", "facility_id", "duplicate_natural_key", "Duplicate record already exists. Enable draft update or change the natural key.", key)

        if not row_errors:
            row_exception_warnings = [w for w in row_warnings if w["issue"] != "will_update_existing_draft"]
            status = "exception" if row_exception_warnings else "draft"
            valid_rows.append(
                {
                    "id": uid("ACT-"),
                    "facility_id": facility_id,
                    "period_start": ps.isoformat() if ps else "",
                    "period_end": pe.isoformat() if pe else "",
                    "scope": scope_v if scope_v is not None else 0,
                    "source_category": source_category,
                    "activity_type": activity_type,
                    "quantity": qty_v if qty_v is not None else 0.0,
                    "unit": unit_v,
                    "measurement_status": measurement_status,
                    "data_quality": data_quality,
                    "source_system": source_system,
                    "provenance": provenance,
                    "status": status,
                    "exception_flags": json.dumps([w["issue"] for w in row_exception_warnings]),
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "schema_version": SCHEMA_VERSION,
                    "is_demo": 0,
                    "_row": row_num,
                    "_key": key or "",
                    "_existing_id": existing_id,
                }
            )

        errors.extend(row_errors)
        errors.extend(row_warnings)

    valid_df = pd.DataFrame(valid_rows, columns=VALID_COLUMNS) if valid_rows else pd.DataFrame(columns=VALID_COLUMNS)
    errors_df = pd.DataFrame(errors, columns=ERROR_COLUMNS) if errors else pd.DataFrame(columns=ERROR_COLUMNS)

    error_rows = int(errors_df[errors_df["severity"] == "error"]["row"].nunique()) if not errors_df.empty else 0
    warning_rows = int(errors_df[errors_df["severity"] == "warning"]["row"].nunique()) if not errors_df.empty else 0

    meta = {
        "total_rows": total_rows,
        "valid_rows": len(valid_df),
        "error_rows": error_rows,
        "warning_rows": warning_rows,
    }
    return valid_df, errors_df, meta


def persist_valid_rows(conn: sqlite3.Connection, valid_df: pd.DataFrame, role: str) -> Tuple[int, int]:
    inserted = 0
    updated = 0
    now = now_iso()

    for _, r in valid_df.iterrows():
        existing_id = r.get("_existing_id")
        if existing_id is not None and safe_str(existing_id):
            conn.execute(
                """
                UPDATE activity_records
                SET facility_id=?, period_start=?, period_end=?, scope=?, source_category=?,
                    activity_type=?, quantity=?, unit=?, measurement_status=?, data_quality=?,
                    source_system=?, provenance=?, status=?, exception_flags=?, updated_at=?,
                    schema_version=?
                WHERE id=? AND status IN ('draft','submitted','exception')
                """,
                (
                    safe_str(r["facility_id"]),
                    safe_str(r["period_start"]),
                    safe_str(r["period_end"]),
                    int(safe_float(r["scope"], 0)),
                    safe_str(r["source_category"]),
                    safe_str(r["activity_type"]).lower(),
                    float(safe_float(r["quantity"], 0.0)),
                    safe_str(r["unit"]),
                    safe_str(r["measurement_status"]).lower(),
                    safe_str(r["data_quality"]).lower(),
                    safe_str(r["source_system"]),
                    safe_str(r["provenance"]),
                    safe_str(r["status"]),
                    safe_str(r["exception_flags"], "[]"),
                    now,
                    SCHEMA_VERSION,
                    safe_str(existing_id),
                ),
            )
            changes = conn.execute("SELECT changes()").fetchone()[0]
            if changes > 0:
                updated += 1
            else:
                raise RuntimeError(f"Could not update existing record {safe_str(existing_id)}. It may have changed concurrently.")
        else:
            insert_row(
                conn,
                "activity_records",
                {
                    "id": safe_str(r["id"]) or uid("ACT-"),
                    "facility_id": safe_str(r["facility_id"]),
                    "period_start": safe_str(r["period_start"]),
                    "period_end": safe_str(r["period_end"]),
                    "scope": int(safe_float(r["scope"], 0)),
                    "source_category": safe_str(r["source_category"]),
                    "activity_type": safe_str(r["activity_type"]).lower(),
                    "quantity": float(safe_float(r["quantity"], 0.0)),
                    "unit": safe_str(r["unit"]),
                    "measurement_status": safe_str(r["measurement_status"]).lower(),
                    "data_quality": safe_str(r["data_quality"]).lower(),
                    "source_system": safe_str(r["source_system"]),
                    "provenance": safe_str(r["provenance"]),
                    "status": safe_str(r["status"]),
                    "exception_flags": safe_str(r["exception_flags"], "[]"),
                    "created_at": safe_str(r["created_at"], now),
                    "updated_at": now,
                    "schema_version": SCHEMA_VERSION,
                    "is_demo": 0,
                },
            )
            inserted += 1

    return inserted, updated


def commit_import(
    conn: sqlite3.Connection,
    valid_df: pd.DataFrame,
    errors_df: pd.DataFrame,
    filename: str,
    checksum: str,
    storage_path: str,
    role: str,
    total_rows: int,
) -> Dict[str, Any]:
    import_id = uid("IMP-")
    rows_valid = len(valid_df)
    error_rows = int(errors_df[errors_df["severity"] == "error"]["row"].nunique()) if not errors_df.empty else 0
    warning_rows = int(errors_df[errors_df["severity"] == "warning"]["row"].nunique()) if not errors_df.empty else 0

    status = "completed"
    if error_rows > 0 and rows_valid == 0:
        status = "failed"
    elif warning_rows > 0 or error_rows > 0:
        status = "completed_with_issues"

    insert_row(
        conn,
        "imports",
        {
            "id": import_id,
            "filename": filename,
            "checksum": checksum,
            "rows_total": total_rows,
            "rows_valid": rows_valid,
            "rows_invalid": error_rows,
            "rows_updated": 0,
            "status": status,
            "created_at": now_iso(),
            "actor_role": role,
            "storage_path": storage_path,
            "notes": json.dumps({"warning_rows": warning_rows}),
        },
    )

    size_bytes = 0
    if storage_path and os.path.exists(storage_path):
        size_bytes = os.path.getsize(storage_path)

    insert_row(
        conn,
        "attachments",
        {
            "id": uid("ATT-"),
            "import_id": import_id,
            "filename": filename,
            "checksum": checksum,
            "size_bytes": size_bytes,
            "storage_path": storage_path,
            "content_type": "text/csv" if filename.lower().endswith(".csv") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "created_at": now_iso(),
        },
    )

    inserted, updated = persist_valid_rows(conn, valid_df, role)

    conn.execute(
        "UPDATE imports SET rows_updated=? WHERE id=?",
        (updated, import_id),
    )

    audit(
        conn,
        role,
        "import_commit",
        "import",
        import_id,
        {
            "filename": filename,
            "checksum": checksum,
            "inserted": inserted,
            "updated": updated,
            "invalid_rows": error_rows,
            "warning_rows": warning_rows,
        },
    )

    return {
        "import_id": import_id,
        "inserted": inserted,
        "updated": updated,
        "invalid_rows": error_rows,
        "warning_rows": warning_rows,
        "status": status,
    }


def get_record_dict(conn: sqlite3.Connection, record_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM activity_records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def change_record_status(
    conn: sqlite3.Connection,
    record_id: str,
    new_status: str,
    role: str,
    reason: str = "",
    clear_exceptions: bool = False,
) -> None:
    rec = get_record_dict(conn, record_id)
    if not rec:
        raise ValueError("Record not found.")

    current = safe_str(rec.get("status")).lower()
    new_status = new_status.lower()

    if new_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Not allowed to move record from {current} to {new_status}.")

    if new_status == "submitted" and not can(role, "submit"):
        raise PermissionError("Role cannot submit records.")
    if new_status == "approved" and not can(role, "approve"):
        raise PermissionError("Role cannot approve records.")
    if new_status == "rejected" and not can(role, "reject"):
        raise PermissionError("Role cannot reject records.")
    if new_status == "archived" and not can(role, "archive"):
        raise PermissionError("Role cannot archive records.")
    if new_status == "draft" and not can(role, "edit_draft"):
        raise PermissionError("Role cannot reopen or correct records.")
    if new_status == "exception" and not can(role, "edit_draft"):
        raise PermissionError("Role cannot move records to exception.")

    if new_status == "rejected" and not safe_str(reason):
        raise ValueError("Rejection requires a reason.")

    flags = "[]" if clear_exceptions or new_status == "approved" else safe_str(rec.get("exception_flags"), "[]")

    conn.execute(
        "UPDATE activity_records SET status=?, exception_flags=?, updated_at=? WHERE id=?",
        (new_status, flags, now_iso(), record_id),
    )

    audit(
        conn,
        role,
        f"record_status_{new_status}",
        "activity_record",
        record_id,
        {"previous_status": current, "reason": reason},
    )


def save_option_override(conn: sqlite3.Connection, option_id: str, override_value: float, role: str, reason: str) -> None:
    if not can(role, "override"):
        raise PermissionError("Role cannot override model or rule outputs.")
    if not safe_str(reason):
        raise ValueError("Override requires a reason.")

    conn.execute(
        "UPDATE reduction_options SET expected_annual_reduction_override=?, updated_at=? WHERE id=?",
        (float(override_value), now_iso(), option_id),
    )
    audit(
        conn,
        role,
        "option_override",
        "reduction_option",
        option_id,
        {"override_tco2e": float(override_value), "reason": reason},
    )


def create_sqlite_backup() -> str:
    ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"emissions_backup_{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def list_backups() -> List[str]:
    if not os.path.exists(BACKUP_DIR):
        return []
    files = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")]
    return sorted([os.path.join(BACKUP_DIR, f) for f in files], reverse=True)


def generate_report(
    conn: sqlite3.Connection,
    baseline_df: pd.DataFrame,
    detail_df: pd.DataFrame,
    exceptions_df: pd.DataFrame,
    evaluated_df: pd.DataFrame,
    portfolio: Tuple[List[str], float, float, float, pd.DataFrame],
    role: str,
    filters: Dict[str, Any],
) -> Tuple[str, str]:
    selected_ids, spent, reduction, opex, feas = portfolio
    selected_df = evaluated_df[evaluated_df["id"].isin(selected_ids)] if not evaluated_df.empty else pd.DataFrame()

    imports_df = fetch_df(
        conn,
        "SELECT filename, checksum, rows_total, rows_valid, rows_invalid, created_at, actor_role FROM imports ORDER BY created_at DESC LIMIT 20",
    )
    factors_df = fetch_df(
        conn,
        "SELECT id, activity_type, unit, value, value_unit, effective_from, effective_to, source_reference, version FROM emission_factors ORDER BY activity_type, effective_from",
    )

    baseline_total = float(baseline_df["tco2e_annual"].sum()) if not baseline_df.empty else 0.0
    baseline_low = float(baseline_df["tco2e_annual_low"].sum()) if not baseline_df.empty else 0.0
    baseline_high = float(baseline_df["tco2e_annual_high"].sum()) if not baseline_df.empty else 0.0

    md = []
    md.append(f"# {APP_NAME} — Decision Support Report")
    md.append("")
    md.append(f"Generated: {now_iso()}  ")
    md.append(f"Application role: {role}  ")
    md.append(f"Engine version: {ENGINE_VERSION}  ")
    md.append(f"Schema version: {SCHEMA_VERSION}  ")
    md.append("")
    md.append("## Important limitation")
    md.append("")
    md.append("This report is generated from synthetic demonstration data unless explicitly replaced with permissioned customer data. It is decision support, not a regulatory certification, not an audited inventory, and not a substitute for qualified practitioner review.")
    md.append("")
    md.append("## 1. Scope and boundary")
    md.append("")
    md.append(f"- Date range: {filters.get('start')} to {filters.get('end')}")
    md.append(f"- Facilities: {', '.join(filters.get('facility_ids', [])) or 'all demo facilities'}")
    md.append(f"- Record statuses included: {', '.join(filters.get('statuses', []))}")
    md.append(f"- Exceptions included: {filters.get('include_exceptions', False)}")
    md.append("- Boundary: Scope 1, 2 and selected Scope 3 industrial activity records available in the local database.")
    md.append("")
    md.append("## 2. Method")
    md.append("")
    md.append("Emissions are calculated deterministically as:")
    md.append("")
    md.append("```text")
    md.append("tCO2e = activity_quantity × dated_emission_factor / unit_conversion")
    md.append("```")
    md.append("")
    md.append("Where factor units are kg CO2e per activity unit, division by 1000 converts to tonnes CO2e. Where factor units are tCO2e per activity unit, no division is applied.")
    md.append("")
    md.append("Annualisation uses the number of distinct calendar months covered by eligible records:")
    md.append("")
    md.append("```text")
    md.append("annual_tCO2e = period_total_tCO2e × 12 / months_covered")
    md.append("```")
    md.append("")
    md.append("Uncertainty is a simple transparent range based on measurement status, data quality, and factor age. It is not a statistically validated uncertainty distribution.")
    md.append("")
    md.append("## 3. Baseline summary")
    md.append("")
    md.append(f"- Baseline annual CO2e: {baseline_total:,.1f} tCO2e/year")
    md.append(f"- Low uncertainty bound: {baseline_low:,.1f} tCO2e/year")
    md.append(f"- High uncertainty bound: {baseline_high:,.1f} tCO2e/year")
    md.append(f"- Eligible aggregated source groups: {len(baseline_df)}")
    md.append(f"- Calculation exceptions: {len(exceptions_df)}")
    md.append("")
    if not baseline_df.empty:
        md.append("### Baseline by source group")
        md.append("")
        md.append("```csv")
        md.append(
            baseline_df[
                [
                    "facility_name",
                    "scope",
                    "source_category",
                    "activity_type",
                    "unit",
                    "tco2e_annual",
                    "tco2e_annual_low",
                    "tco2e_annual_high",
                    "factor_sources",
                    "avg_uncertainty",
                ]
            ].to_csv(index=False)
        )
        md.append("```")
    md.append("")
    md.append("## 4. Reduction options")
    md.append("")
    if not evaluated_df.empty:
        md.append("```csv")
        md.append(
            evaluated_df[
                [
                    "id",
                    "facility_id",
                    "name",
                    "category",
                    "capex",
                    "opex_savings_annual",
                    "implementation_months",
                    "eligible_baseline_annual",
                    "reduction_annual",
                    "cost_per_tco2e",
                    "confidence",
                    "explanation",
                ]
            ].to_csv(index=False)
        )
        md.append("```")
    else:
        md.append("No reduction options available.")
    md.append("")
    md.append("## 5. Recommended portfolio under constraints")
    md.append("")
    md.append(f"- Budget: {filters.get('budget'):,.0f}")
    md.append(f"- Maximum implementation months: {filters.get('max_months')}")
    md.append(f"- Minimum confidence: {filters.get('min_confidence')}")
    md.append(f"- Selected options: {len(selected_ids)}")
    md.append(f"- Capital deployed: {spent:,.0f}")
    md.append(f"- Expected annual reduction: {reduction:,.1f} tCO2e/year")
    md.append(f"- Expected annual operating savings: {opex:,.0f}")
    md.append(f"- Average cost per tCO2e: {(spent / reduction if reduction > 0 else float('inf')):,.2f}")
    md.append("")
    if not selected_df.empty:
        md.append("```csv")
        md.append(selected_df[["id", "name", "capex", "reduction_annual", "cost_per_tco2e", "explanation"]].to_csv(index=False))
        md.append("```")
    md.append("")
    md.append("## 6. Data lineage and reference versions")
    md.append("")
    md.append("### Recent imports")
    md.append("")
    if not imports_df.empty:
        md.append("```csv")
        md.append(imports_df.to_csv(index=False))
        md.append("```")
    else:
        md.append("No imports recorded yet.")
    md.append("")
    md.append("### Emission factor catalog")
    md.append("")
    md.append("```csv")
    md.append(factors_df.to_csv(index=False))
    md.append("```")
    md.append("")
    md.append("## 7. Exceptions and data quality warnings")
    md.append("")
    if not exceptions_df.empty:
        md.append("```csv")
        md.append(exceptions_df.to_csv(index=False))
        md.append("```")
    else:
        md.append("No calculation exceptions in the selected scope.")
    md.append("")
    md.append("## 8. Human approval boundary")
    md.append("")
    md.append("Approved records are those explicitly moved to `approved` by a reviewer role. Draft, submitted, and exception records are provisional. Manual overrides are audited and must include a reason.")
    md.append("")

    md_text = "\n".join(md)

    html_parts = [
        "<html><head><meta charset='utf-8'>",
        f"<title>{APP_NAME} Report</title>",
        "<style>body{font-family:Arial,sans-serif;margin:24px;} table{border-collapse:collapse;margin:16px 0;} th,td{border:1px solid #ddd;padding:6px 8px;font-size:12px;} th{background:#f5f5f5;} .warn{color:#8a6d3b;}</style>",
        "</head><body>",
        f"<h1>{APP_NAME}</h1>",
        f"<p><strong>Generated:</strong> {now_iso()}<br/><strong>Role:</strong> {role}<br/><strong>Engine:</strong> {ENGINE_VERSION}<br/><strong>Schema:</strong> {SCHEMA_VERSION}</p>",
        "<div class='warn'><strong>Limitation:</strong> Synthetic demo output unless replaced with permissioned data. Decision support only; not regulatory certification.</div>",
        "<h2>Baseline summary</h2>",
        f"<p>Annual CO2e: <strong>{baseline_total:,.1f}</strong> tCO2e/year</p>",
        "<h2>Baseline table</h2>",
        baseline_df.to_html(index=False) if not baseline_df.empty else "<p>No baseline rows.</p>",
        "<h2>Reduction options</h2>",
        evaluated_df[["id", "name", "facility_id", "capex", "reduction_annual", "cost_per_tco2e", "confidence", "explanation"]].to_html(index=False) if not evaluated_df.empty else "<p>No options.</p>",
        "<h2>Recommended portfolio</h2>",
        selected_df[["id", "name", "capex", "reduction_annual", "cost_per_tco2e"]].to_html(index=False) if not selected_df.empty else "<p>No selected options.</p>",
        "<h2>Exceptions</h2>",
        exceptions_df.to_html(index=False) if not exceptions_df.empty else "<p>No exceptions.</p>",
        "<h2>Factor catalog</h2>",
        factors_df.to_html(index=False),
        "</body></html>",
    ]
    html_text = "\n".join(html_parts)

    audit(conn, role, "report_export", "report", uid("RPT-"), {"baseline_rows": len(baseline_df), "selected_options": len(selected_ids)})

    return md_text, html_text


def run_self_tests() -> pd.DataFrame:
    results: List[Dict[str, Any]] = []

    def add(name: str, passed: bool, message: str = "") -> None:
        results.append({"test": name, "pass": bool(passed), "message": message})

    try:
        with db_conn() as conn:
            fac_count = conn.execute("SELECT COUNT(*) FROM facilities WHERE is_demo=1").fetchone()[0]
            act_count = conn.execute("SELECT COUNT(*) FROM activity_records").fetchone()[0]
            factor_count = conn.execute("SELECT COUNT(*) FROM emission_factors").fetchone()[0]
            add("demo_seed_present", fac_count >= 3 and act_count > 50 and factor_count >= 6, f"facilities={fac_count}, activities={act_count}, factors={factor_count}")

            calc = emissions_from_quantity(100, "MWh", 202, "kg CO2e/MWh")
            add("hand_calc_natural_gas", abs(calc - 20.2) < 1e-9, f"100 MWh × 202 kg/MWh = {calc:.3f} tCO2e")

            old_date = (TODAY - timedelta(days=500)).isoformat()
            new_date = (TODAY - timedelta(days=10)).isoformat()
            f_old = select_factor(conn, "grid_electricity", "MWh", old_date)
            f_new = select_factor(conn, "grid_electricity", "MWh", new_date)
            add(
                "dated_factor_selection",
                bool(f_old and f_new and f_old["value"] != f_new["value"]),
                f"old={f_old['value'] if f_old else None}, new={f_new['value'] if f_new else None}",
            )

            bad = pd.DataFrame(
                [
                    {
                        "facility_id": "FAC-STL-001",
                        "period_start": (TODAY - timedelta(days=40)).isoformat(),
                        "period_end": (TODAY - timedelta(days=10)).isoformat(),
                        "scope": 1,
                        "source_category": "Stationary combustion",
                        "activity_type": "lpg_combustion",
                        "quantity": "-10",
                        "unit": "litres",
                        "measurement_status": "measured",
                        "data_quality": "high",
                        "source_system": "TEST",
                        "provenance": "TEST",
                    }
                ]
            ).astype(str)
            valid_bad, errors_bad, meta_bad = validate_activity_dataframe(bad, conn, allow_update=False)
            add(
                "validation_negative_rejected",
                valid_bad.empty and not errors_bad.empty and (errors_bad["severity"] == "error").any(),
                f"errors={len(errors_bad)}",
            )

            dup = pd.DataFrame(
                [
                    {
                        "facility_id": "FAC-STL-001",
                        "period_start": (TODAY - timedelta(days=40)).isoformat(),
                        "period_end": (TODAY - timedelta(days=10)).isoformat(),
                        "scope": 1,
                        "source_category": "Stationary combustion",
                        "activity_type": "lpg_combustion",
                        "quantity": "10",
                        "unit": "litres",
                        "measurement_status": "measured",
                        "data_quality": "high",
                        "source_system": "TEST",
                        "provenance": "TEST",
                    },
                    {
                        "facility_id": "FAC-STL-001",
                        "period_start": (TODAY - timedelta(days=40)).isoformat(),
                        "period_end": (TODAY - timedelta(days=10)).isoformat(),
                        "scope": 1,
                        "source_category": "Stationary combustion",
                        "activity_type": "lpg_combustion",
                        "quantity": "12",
                        "unit": "litres",
                        "measurement_status": "measured",
                        "data_quality": "high",
                        "source_system": "TEST",
                        "provenance": "TEST",
                    },
                ]
            ).astype(str)
            _, errors_dup, _ = validate_activity_dataframe(dup, conn, allow_update=False)
            add(
                "validation_duplicate_within_file_rejected",
                (errors_dup["issue"] == "duplicate_within_file").any(),
                "Duplicate natural key inside file detected",
            )

            draft_row = conn.execute(
                "SELECT * FROM activity_records WHERE status='draft' LIMIT 1"
            ).fetchone()
            if draft_row:
                d = dict(draft_row)
                update_df = pd.DataFrame(
                    [
                        {
                            "facility_id": d["facility_id"],
                            "period_start": d["period_start"],
                            "period_end": d["period_end"],
                            "scope": d["scope"],
                            "source_category": d["source_category"],
                            "activity_type": d["activity_type"],
                            "quantity": str(float(d["quantity"]) + 1),
                            "unit": d["unit"],
                            "measurement_status": d["measurement_status"],
                            "data_quality": d["data_quality"],
                            "source_system": d["source_system"] or "TEST",
                            "provenance": d["provenance"] or "TEST",
                        }
                    ]
                ).astype(str)
                valid_upd, errors_upd, _ = validate_activity_dataframe(update_df, conn, allow_update=True)
                add(
                    "safe_reimport_updates_draft_only",
                    not valid_upd.empty and (errors_upd["issue"] == "will_update_existing_draft").any(),
                    "Draft record can be updated safely",
                )
            else:
                add("safe_reimport_updates_draft_only", False, "No draft record found for test")

            add("permission_analyst_cannot_approve", not can("analyst/operator", "approve"), "Analyst cannot approve")
            add("permission_reviewer_can_approve", can("reviewer", "approve"), "Reviewer can approve")
            add("permission_stakeholder_read_only", can("read-only stakeholder", "view") and not can("read-only stakeholder", "create"), "Stakeholder is read-only")

            fake_eval = pd.DataFrame(
                [
                    {"id": "A", "capex": 100, "implementation_months": 6, "confidence": 0.8, "reduction_annual": 20, "cost_per_tco2e": 5, "status": "submitted", "opex_savings_annual": 10},
                    {"id": "B", "capex": 180, "implementation_months": 12, "confidence": 0.7, "reduction_annual": 50, "cost_per_tco2e": 3.6, "status": "draft", "opex_savings_annual": 20},
                    {"id": "C", "capex": 50, "implementation_months": 30, "confidence": 0.9, "reduction_annual": 10, "cost_per_tco2e": 5, "status": "submitted", "opex_savings_annual": 5},
                ]
            )
            selected, spent, red, opex_save, feas = optimize_portfolio(fake_eval, budget=200, max_months=12, min_confidence=0.6)
            constraint_ok = spent <= 200 and all(fake_eval[fake_eval["id"].isin(selected)]["implementation_months"] <= 12)
            add("optimization_hard_constraints", constraint_ok and len(selected) > 0, f"selected={selected}, spent={spent}")

            selected_none, spent_none, _, _, _ = optimize_portfolio(fake_eval, budget=0, max_months=12, min_confidence=0.6)
            add("optimization_infeasible_returns_empty", len(selected_none) == 0 and spent_none == 0, "Zero budget yields no feasible selection")

            baseline, detail, exceptions = build_baseline_df(conn, statuses=("approved", "submitted"))
            options = load_options(conn)
            evaluated = evaluate_options(options, baseline, use_overrides=False)
            portfolio = optimize_portfolio(evaluated, budget=1_000_000, max_months=24, min_confidence=0.5)
            md, html = generate_report(
                conn,
                baseline,
                detail,
                exceptions,
                evaluated,
                portfolio,
                "administrator",
                {
                    "start": (TODAY - timedelta(days=365)).isoformat(),
                    "end": TODAY.isoformat(),
                    "facility_ids": [],
                    "statuses": ["approved", "submitted"],
                    "include_exceptions": False,
                    "budget": 1_000_000,
                    "max_months": 24,
                    "min_confidence": 0.5,
                },
            )
            add("report_contains_method", "Emissions are calculated deterministically" in md and "tCO2e" in html, "Report includes method and units")
            add("report_traceability", "factor_sources" in baseline.columns or baseline.empty, "Baseline includes factor source traceability")

            samples = monte_carlo_baseline(baseline, iterations=50, seed=7)
            post = simulate_post_samples(samples, portfolio[2], float(baseline["tco2e_annual"].sum()) if not baseline.empty else 0.0)
            add("monte_carlo_runs", len(samples) == 50 and len(post) == 50, "Stochastic simulation produces samples")

    except Exception as exc:
        add("unexpected_error", False, str(exc))

    return pd.DataFrame(results)


def run_tests_headless() -> None:
    df = run_self_tests()
    print(df.to_string(index=False))
    if not df["pass"].all():
        raise SystemExit(1)


PRODUCT_BRIEF = """
# Product brief

Problem: Sustainability and environmental teams spend significant time collecting scattered facility activity data,
checking units and dates, applying emission factors manually, and building reduction scenarios in spreadsheets.
The process is slow, error-prone, and difficult to audit.

Initial narrow workflow: Import facility activity data, validate it, calculate CO2e emissions using dated factors,
compare reduction options, and export a reviewer-ready decision-support report.

Primary users: analyst/operator enters and validates data; reviewer approves records and overrides; administrator
manages reference data and backups; read-only stakeholder consumes reports.

First release: local Windows MVP with SQLite, synthetic demo data, CSV/XLSX import, deterministic emissions engine,
Streamlit UI, Plotly charts, audit trail, and exportable report. No paid API keys.

Success criteria: a new user can run the app, import the valid sample file, see rejected rows in the invalid sample,
run the baseline and reduction simulation, and export a traceable report. Automated tests pass.
"""

FUNCTIONAL_REQUIREMENTS = """
# Functional requirements

FR-1 The system shall initialize a local SQLite database with versioned schema.
FR-2 The system shall load clearly labelled synthetic demo facilities, activity records, dated emission factors, and reduction options.
FR-3 The system shall import CSV and XLSX activity files with preview, row-level validation, and downloadable error reports.
FR-4 The system shall reject malformed rows for missing required columns, invalid dates, negative quantities, unknown facilities, unsupported units, and duplicate natural keys.
FR-5 The system shall preserve original uploaded files and store checksums and metadata.
FR-6 The system shall calculate emissions deterministically using activity quantity and dated emission factors.
FR-7 The system shall show formula, factor source, factor version, effective dates, unit conversion, and data-quality warnings for each calculation.
FR-8 The system shall support record lifecycle states: draft, submitted, approved, rejected, exception, archived.
FR-9 The system shall enforce role-based actions in application logic: analyst submits, reviewer approves/rejects, administrator archives/manages, stakeholder reads/exports.
FR-10 The system shall evaluate reduction options using activity reduction, factor improvement, capture rate, capex, opex savings, implementation time, and confidence.
FR-11 The system shall rank options and select a feasible portfolio under budget, schedule, and confidence constraints.
FR-12 The system shall run a reproducible Monte Carlo uncertainty simulation for baseline and post-action emissions.
FR-13 The system shall export baseline CSV, options CSV, exceptions CSV, audit CSV, Markdown report, and HTML report.
FR-14 The system shall record audit events for imports, status changes, overrides, and exports.
FR-15 The system shall provide backup download and documented restore procedure.
"""

BACKLOG = """
# Prioritized backlog

## MVP
- Local SQLite schema and synthetic demo data.
- CSV/XLSX import with preview and row-level errors.
- Deterministic emissions factor calculator with dated factors.
- Record lifecycle and role-based review actions.
- Reduction option evaluation and constrained portfolio ranking.
- Streamlit dashboard with actionable charts and explanation panels.
- Report export and audit log.
- Automated self-tests.

## Post-MVP
- Real authentication and SSO.
- PostgreSQL multi-user deployment.
- Supplier declaration portal.
- Document evidence workflow with OCR-free metadata extraction.
- Scheduled ingestion from historian/MES/ERP.
- Advanced material-flow and water accounting.
- Formal uncertainty model calibrated with customer data.
- Regression benchmark suite against authoritative factor sets.

## Enterprise
- Directory integration and granular facility permissions.
- Audit-grade immutable event store.
- SIEM/log integration.
- High-availability deployment.
- Customer-managed encryption keys.
- Regulatory reporting packs prepared for qualified review.
- Paid integration services and support SLAs.
"""

ACCEPTANCE_CHECKLIST = """
# Measurable acceptance checklist

Pass/fail conditions:

1. Install and run
   - PASS: README PowerShell commands install dependencies and start Streamlit on Windows.
   - FAIL: App does not start or requires paid API key.

2. Demo loads immediately
   - PASS: First screen shows populated KPIs, charts, and simulation results without blank state.
   - FAIL: User must manually create data before seeing a working dashboard.

3. Valid sample import
   - PASS: valid_activity_sample.csv imports with zero blocking errors and creates draft records.
   - FAIL: valid sample is rejected without actionable reason.

4. Invalid sample handling
   - PASS: invalid_activity_sample.csv produces row-level errors for unknown facility, negative quantity, bad date, duplicate, missing unit, future date.
   - FAIL: malformed rows are silently accepted or errors are not downloadable.

5. Reproducible calculation
   - PASS: 100 MWh natural gas at 202 kg CO2e/MWh yields 20.2 tCO2e in tests.
   - FAIL: calculation changes without documented engine/version change.

6. Traceable explanation
   - PASS: Each record/report row shows factor ID, source reference, effective dates, formula, and data-quality warnings.
   - FAIL: Outputs cannot be traced to inputs and factor versions.

7. Review workflow
   - PASS: analyst can submit; reviewer can approve/reject with reason; stakeholder cannot modify.
   - FAIL: role permissions are not enforced in application logic.

8. Reduction planning
   - PASS: portfolio respects budget, max months, minimum confidence, and reports expected tCO2e reduction.
   - FAIL: selected options violate hard constraints.

9. Export
   - PASS: baseline CSV, options CSV, exceptions CSV, audit CSV, HTML and Markdown report download.
   - FAIL: exports missing or unreadable.

10. Automated tests
   - PASS: QA tab shows all tests pass.
   - FAIL: any required test fails.
"""

RISK_REGISTER = """
# Risk register

Domain validity risk:
- Synthetic factors are not regulatory factors. Mitigation: label clearly, require qualified review before real use.

Data quality risk:
- Missing, late, contradictory, or out-of-range activity data. Mitigation: validation, exception queue, provenance, reviewer approval.

Security risk:
- Role selector is demo-only and not authenticated. Mitigation: production requires SSO, server-side authorization, encrypted transport.

Adoption risk:
- Customers may not trust automated emissions outputs. Mitigation: explainable formulas, audit trail, human approval boundary, pilot with domain expert.

Operational risk:
- Local SQLite file can be corrupted or lost. Mitigation: backup download, restore procedure, versioned schema, migration path to PostgreSQL.

License/commercial risk:
- Open-source dependencies must remain commercially compatible. Mitigation: SBOM, license inventory, pinned production dependencies outside this demo file.
"""

DATA_DICTIONARY = """
# Data dictionary (core fields)

facilities
- id: stable facility identifier.
- name, sector, location, timezone: organizational reference data.
- status: active/inactive.
- provenance, is_demo: source lineage and synthetic flag.

activity_records
- id: stable record identifier.
- facility_id: FK to facilities.
- period_start, period_end: ISO dates defining reporting period.
- scope: 1, 2, or 3.
- source_category: business emission category.
- activity_type: cataloged activity, e.g. natural_gas, grid_electricity.
- quantity: measured or estimated activity amount.
- unit: must match emission factor catalog.
- measurement_status: measured or estimated.
- data_quality: high, medium, low.
- source_system, provenance: lineage.
- status: draft, submitted, approved, rejected, exception, archived.
- exception_flags: JSON list of validation/calculation warnings.
- schema_version: import/calculus schema version.

emission_factors
- id, activity_type, unit, pollutant: lookup key.
- value, value_unit: factor magnitude and unit.
- effective_from, effective_to: dated applicability.
- source_reference, version, jurisdiction: provenance.

reduction_options
- id, facility_id, name, category, description.
- capex, opex_savings_annual, implementation_months.
- applies_scope, applies_activity_type: eligibility.
- activity_reduction_pct, factor_improvement_pct, capture_rate_pct: levers.
- expected_annual_reduction_override: audited manual override.
- confidence, status, owner, evidence.

audit_events
- ts, actor_role, action, entity_type, entity_id, details.

imports / attachments
- filename, checksum, rows_total, rows_valid, rows_invalid, rows_updated, status, storage_path.
"""

ERD_TEXT = """
# Entity relationship summary

facilities 1 ── N activity_records
facilities 1 ── N reduction_options
emission_factors N ── 1 activity_type/unit lookup used by activity_records
imports 1 ── N attachments
imports / activity_records / reduction_options ── N audit_events

Every derived result can be traced to:
- source file or manual entry,
- activity record ID,
- facility ID,
- period dates,
- emission factor ID and version,
- calculation engine version,
- reviewer/approval audit event.
"""


def page_live_demo() -> None:
    st.header("Live demo — synthetic industrial emissions planner")
    st.caption(
        "This screen is populated automatically. The app created a local SQLite database, loaded labelled synthetic "
        "facility activity data, calculated a baseline, ranked reduction options, and ran a Monte Carlo simulation. "
        "No paid API key is used."
    )

    with db_conn() as conn:
        baseline, detail, exceptions = build_baseline_df(conn, statuses=("approved", "submitted"))
        options = load_options(conn)
        evaluated = evaluate_options(options, baseline, use_overrides=False)
        portfolio = optimize_portfolio(evaluated, budget=1_000_000, max_months=24, min_confidence=0.5)
        iterations = int(st.session_state.get("mc_iterations", 300))
        seed = int(st.session_state.get("mc_seed", 12))
        samples = monte_carlo_baseline(baseline, iterations=iterations, seed=seed)
        baseline_mean = float(baseline["tco2e_annual"].sum()) if not baseline.empty else 0.0
        post_samples = simulate_post_samples(samples, portfolio[2], baseline_mean)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Baseline annual CO2e", f"{baseline_mean:,.0f} tCO2e/y")
    c2.metric("Selected options", f"{len(portfolio[0])}")
    c3.metric("Portfolio reduction", f"{portfolio[2]:,.0f} tCO2e/y")
    c4.metric("Capital deployed", f"{portfolio[1]:,.0f}")
    c5.metric("Data exceptions", f"{len(exceptions)}")

    st.divider()
    left, right = st.columns([2, 1])

    with left:
        if not baseline.empty:
            top = baseline.groupby("source_category", as_index=False)["tco2e_annual"].sum().sort_values("tco2e_annual", ascending=False)
            fig1 = px.bar(
                top,
                x="source_category",
                y="tco2e_annual",
                title="Annual CO2e by source category",
                labels={"source_category": "Source category", "tco2e_annual": "tCO2e/year"},
                text_auto=".1f",
            )
            fig1.update_traces(marker_color="#2c7fb8")
            st.plotly_chart(fig1, use_container_width=True)
        else:
            st.info("No baseline rows available.")

        if not detail.empty:
            trend = detail.copy()
            trend["month"] = trend["period_start"].dt.to_period("M").astype(str)
            trend = trend.groupby("month", as_index=False)["tco2e"].sum().sort_values("month")
            fig2 = px.line(
                trend,
                x="month",
                y="tco2e",
                markers=True,
                title="Monthly recorded CO2e (approved + submitted)",
                labels={"month": "Month", "tco2e": "tCO2e"},
            )
            fig2.update_traces(line_color="#1f77b4")
            st.plotly_chart(fig2, use_container_width=True)

    with right:
        st.subheader("Stochastic simulation")
        st.write(
            "The simulation samples each aggregated source within its transparent uncertainty range and applies the "
            "selected reduction portfolio fraction."
        )
        if st.button("Re-run simulation with new seed"):
            st.session_state.mc_seed = int(st.session_state.get("mc_seed", 12)) + 1
            st.rerun()

        base_stats = percentile_stats(samples)
        post_stats = percentile_stats(post_samples)

        st.metric("Baseline P50", f"{base_stats['p50']:,.0f} tCO2e/y")
        st.metric("Post-action P50", f"{post_stats['p50']:,.0f} tCO2e/y")
        st.metric("Expected reduction P50", f"{base_stats['p50'] - post_stats['p50']:,.0f} tCO2e/y")

        fig3 = go.Figure()
        fig3.add_histogram(x=samples, name="Baseline", opacity=0.55, marker_color="#2c7fb8")
        fig3.add_histogram(x=post_samples, name="With portfolio", opacity=0.55, marker_color="#2ca02c")
        fig3.update_layout(
            title="Monte Carlo annual CO2e distribution",
            xaxis_title="tCO2e/year",
            yaxis_title="Simulations",
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig3, use_container_width=True)

    st.divider()
    st.subheader("Top reduction options in demo portfolio")
    selected_ids, spent, reduction, opex, feas = portfolio
    if not evaluated.empty and selected_ids:
        selected_df = evaluated[evaluated["id"].isin(selected_ids)][
            ["name", "facility_id", "category", "capex", "reduction_annual", "cost_per_tco2e", "implementation_months", "confidence"]
        ].copy()
        selected_df["cost_per_tco2e"] = selected_df["cost_per_tco2e"].replace([np.inf, -np.inf], np.nan)
        st.dataframe(selected_df, use_container_width=True, hide_index=True)
    else:
        st.info("No feasible options under the demo budget and constraints.")

    st.caption("Synthetic data only. Not for regulatory filing or certified inventory reporting.")


def page_import() -> None:
    st.header("Import activity data")
    st.caption("CSV and XLSX are supported. The app preserves original files, validates rows, and never silently overwrites approved records.")

    valid_path = os.path.join(SAMPLE_DIR, "valid_activity_sample.csv")
    invalid_path = os.path.join(SAMPLE_DIR, "invalid_activity_sample.csv")

    c1, c2 = st.columns(2)
    if os.path.exists(valid_path):
        with open(valid_path, "rb") as f:
            c1.download_button("Download valid sample CSV", f.read(), file_name="valid_activity_sample.csv", mime="text/csv")
    if os.path.exists(invalid_path):
        with open(invalid_path, "rb") as f:
            c2.download_button("Download invalid sample CSV", f.read(), file_name="invalid_activity_sample.csv", mime="text/csv")

    allow_update = st.checkbox("Allow safe update of existing draft/submitted/exception rows", value=False)
    uploaded = st.file_uploader("Upload activity CSV or XLSX", type=["csv", "xlsx", "xls"])

    if uploaded is not None:
        data = uploaded.getvalue()
        checksum = hashlib.sha256(data).hexdigest()

        if st.session_state.get("import_checksum") != checksum:
            try:
                raw_df = read_tabular_bytes(uploaded.name, data)
                storage_path = store_original_file(uploaded.name, data, checksum)
                with db_conn() as conn:
                    valid_df, errors_df, meta = validate_activity_dataframe(raw_df, conn, allow_update=allow_update)
                st.session_state.import_preview = {
                    "valid": valid_df,
                    "errors": errors_df,
                    "meta": meta,
                    "filename": uploaded.name,
                    "checksum": checksum,
                    "storage_path": storage_path,
                    "allow_update": allow_update,
                }
                st.session_state.import_checksum = checksum
            except Exception as exc:
                st.error(f"Could not read file: {exc}")
                st.session_state.import_preview = None

        preview = st.session_state.get("import_preview")
        if preview:
            st.subheader("Import preview")
            meta = preview["meta"]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total rows", meta["total_rows"])
            m2.metric("Valid rows", meta["valid_rows"])
            m3.metric("Blocking error rows", meta["error_rows"])
            m4.metric("Warning rows", meta["warning_rows"])

            errors_df = preview["errors"]
            if not errors_df.empty:
                st.markdown("#### Validation issues")
                st.dataframe(errors_df, use_container_width=True, hide_index=True)
                err_csv = errors_df.to_csv(index=False).encode("utf-8")
                st.download_button("Download error report CSV", err_csv, file_name="import_error_report.csv", mime="text/csv")
            else:
                st.success("No blocking errors or warnings detected.")

            valid_df = preview["valid"]
            if not valid_df.empty:
                st.markdown("#### Rows ready to import")
                display_cols = [
                    "facility_id", "period_start", "period_end", "scope", "source_category",
                    "activity_type", "quantity", "unit", "measurement_status", "data_quality",
                    "status", "exception_flags"
                ]
                st.dataframe(valid_df[display_cols], use_container_width=True, hide_index=True)

            b1, b2 = st.columns(2)
            with b1:
                if st.button("Import valid rows", disabled=valid_df.empty, type="primary"):
                    with db_conn() as conn:
                        summary = commit_import(
                            conn,
                            valid_df,
                            errors_df,
                            preview["filename"],
                            preview["checksum"],
                            preview["storage_path"],
                            st.session_state.role,
                            meta["total_rows"],
                        )
                    st.success(
                        f"Import complete. Inserted {summary['inserted']}, updated {summary['updated']}, "
                        f"invalid rows {summary['invalid_rows']}, warnings {summary['warning_rows']}."
                    )
                    st.session_state.pop("import_preview", None)
                    st.session_state.pop("import_checksum", None)
                    st.rerun()
            with b2:
                if st.button("Cancel preview"):
                    st.session_state.pop("import_preview", None)
                    st.session_state.pop("import_checksum", None)
                    st.rerun()


def page_records() -> None:
    st.header("Records and lifecycle")
    role = st.session_state.role

    with db_conn() as conn:
        fac_df = fetch_df(conn, "SELECT id, name FROM facilities ORDER BY name")
        status_options = ["draft", "submitted", "approved", "exception", "rejected", "archived"]

        fac_sel = st.multiselect("Facilities", fac_df["id"].tolist(), default=[])
        status_sel = st.multiselect("Status", status_options, default=["draft", "submitted", "approved", "exception"])
        search = st.text_input("Search record ID, activity type, source system")

        q = """
            SELECT a.*, f.name AS facility_name
            FROM activity_records a
            JOIN facilities f ON f.id=a.facility_id
            WHERE 1=1
        """
        params: List[Any] = []
        if fac_sel:
            q += f" AND a.facility_id IN ({', '.join(['?'] * len(fac_sel))})"
            params.extend(fac_sel)
        if status_sel:
            q += f" AND a.status IN ({', '.join(['?'] * len(status_sel))})"
            params.extend(status_sel)
        if search:
            q += " AND (a.id LIKE ? OR a.activity_type LIKE ? OR a.source_system LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        q += " ORDER BY a.period_end DESC, a.facility_id, a.activity_type"

        records = fetch_df(conn, q, tuple(params))

        show_cols = [
            "id", "facility_name", "period_end", "scope", "source_category", "activity_type",
            "quantity", "unit", "measurement_status", "data_quality", "status", "source_system"
        ]
        st.dataframe(records[show_cols] if not records.empty else records, use_container_width=True, hide_index=True)

        if not records.empty:
            selected_id = st.selectbox("Select record", records["id"].tolist())
            selected_row = records[records["id"] == selected_id].iloc[0].to_dict()
            rec_base = get_record_dict(conn, selected_id)
            if rec_base:
                rec = {**selected_row, **rec_base}
                st.subheader(f"Record {selected_id}")
                rc1, rc2 = st.columns([2, 1])
                with rc1:
                    st.json(
                        {
                            "facility": rec.get("facility_name"),
                            "period": f"{rec.get('period_start')} to {rec.get('period_end')}",
                            "scope": rec.get("scope"),
                            "source_category": rec.get("source_category"),
                            "activity_type": rec.get("activity_type"),
                            "quantity": rec.get("quantity"),
                            "unit": rec.get("unit"),
                            "measurement_status": rec.get("measurement_status"),
                            "data_quality": rec.get("data_quality"),
                            "status": rec.get("status"),
                            "exception_flags": json.loads(rec.get("exception_flags") or "[]"),
                            "source_system": rec.get("source_system"),
                            "provenance": rec.get("provenance"),
                        }
                    )
                with rc2:
                    calc, flags = calculate_record_emissions(conn, rec)
                    if calc:
                        st.metric("Calculated CO2e", f"{calc['tco2e']:,.2f} tCO2e")
                        st.metric("Uncertainty", f"{calc['uncertainty_pct']:.1f}%")
                        st.write("**Formula**")
                        st.code(calc["formula"])
                        st.write("**Factor provenance**")
                        st.json(
                            {
                                "factor_id": calc["factor_id"],
                                "value": calc["factor_value"],
                                "value_unit": calc["factor_unit"],
                                "source": calc["factor_source"],
                                "version": calc["factor_version"],
                                "effective_from": calc["factor_effective_from"],
                                "effective_to": calc["factor_effective_to"],
                            }
                        )
                    else:
                        st.warning(f"Cannot calculate: {', '.join(flags)}")

                st.markdown("#### Workflow actions")
                ac1, ac2, ac3 = st.columns(3)

                with ac1:
                    if can(role, "submit") and rec.get("status") in ("draft",):
                        if st.button("Submit for review"):
                            change_record_status(conn, selected_id, "submitted", role, "Submitted by analyst")
                            st.rerun()

                    if can(role, "edit_draft") and rec.get("status") in ("draft", "exception"):
                        with st.form("edit_record_form"):
                            st.write("Correct record")
                            new_qty = st.number_input("Quantity", value=float(rec.get("quantity", 0.0)), step=1.0)
                            new_measure = st.selectbox(
                                "Measurement status",
                                ["measured", "estimated"],
                                index=0 if safe_str(rec.get("measurement_status")).lower() == "measured" else 1
                            )
                            new_dq = st.selectbox(
                                "Data quality",
                                ["high", "medium", "low"],
                                index={"high": 0, "medium": 1, "low": 2}.get(safe_str(rec.get("data_quality")).lower(), 1)
                            )
                            new_source = st.text_input("Source system", value=safe_str(rec.get("source_system")))
                            new_prov = st.text_input("Provenance", value=safe_str(rec.get("provenance")))
                            reason = st.text_input("Correction reason")
                            submitted = st.form_submit_button("Save correction")
                            if submitted:
                                current_status = safe_str(rec.get("status")).lower()
                                edit_df = pd.DataFrame(
                                    [
                                        {
                                            "facility_id": rec.get("facility_id"),
                                            "period_start": rec.get("period_start"),
                                            "period_end": rec.get("period_end"),
                                            "scope": rec.get("scope"),
                                            "source_category": rec.get("source_category"),
                                            "activity_type": rec.get("activity_type"),
                                            "quantity": new_qty,
                                            "unit": rec.get("unit"),
                                            "measurement_status": new_measure,
                                            "data_quality": new_dq,
                                            "source_system": new_source,
                                            "provenance": new_prov,
                                        }
                                    ]
                                ).astype(str)
                                valid_edit, errors_edit, _ = validate_activity_dataframe(edit_df, conn, allow_update=True)
                                if not errors_edit[errors_edit["severity"] == "error"].empty:
                                    st.error("Correction blocked by validation errors.")
                                    st.dataframe(errors_edit, use_container_width=True)
                                elif not valid_edit.empty:
                                    persist_valid_rows(conn, valid_edit, role)
                                    new_status = safe_str(valid_edit.iloc[0].get("status")).lower()
                                    if new_status != current_status:
                                        try:
                                            change_record_status(
                                                conn,
                                                selected_id,
                                                new_status,
                                                role,
                                                reason or f"Corrected and moved to {new_status}",
                                                clear_exceptions=(new_status == "draft"),
                                            )
                                        except Exception as exc:
                                            st.error(f"Record updated but status change failed: {exc}")
                                    audit(conn, role, "record_corrected", "activity_record", selected_id, {"reason": reason, "new_status": new_status})
                                    st.success("Record corrected.")
                                    st.rerun()

                with ac2:
                    if can(role, "approve") and rec.get("status") == "submitted":
                        approve_reason = st.text_input("Approval note (optional)", key=f"approve_{selected_id}")
                        if st.button("Approve record"):
                            change_record_status(conn, selected_id, "approved", role, approve_reason, clear_exceptions=True)
                            st.rerun()

                    if can(role, "reject") and rec.get("status") in ("submitted", "exception"):
                        reject_reason = st.text_input("Rejection reason", key=f"reject_{selected_id}")
                        if st.button("Reject record"):
                            try:
                                change_record_status(conn, selected_id, "rejected", role, reject_reason)
                                st.rerun()
                            except Exception as exc:
                                st.error(str(exc))

                with ac3:
                    if can(role, "archive") and rec.get("status") == "approved":
                        if st.button("Archive record"):
                            change_record_status(conn, selected_id, "archived", role, "Archived by administrator")
                            st.rerun()

        st.divider()
        st.markdown("#### Create manual activity record")
        if not can(role, "create"):
            st.info("Current role cannot create records.")
        else:
            facility_choices = fac_df["id"].tolist()
            known_units = get_known_activity_units(conn)
            activity_choices = sorted(known_units.keys())

            mc1, mc2, mc3 = st.columns(3)
            m_fac = mc1.selectbox("Facility", facility_choices)
            m_start = mc2.date_input("Period start", value=TODAY - timedelta(days=30))
            m_end = mc3.date_input("Period end", value=TODAY - timedelta(days=1))

            ms1, ms2, ms3 = st.columns(3)
            m_scope = ms1.selectbox("Scope", [1, 2, 3])
            m_cat = ms2.text_input("Source category", value="Stationary combustion")
            m_activity = ms3.selectbox("Activity type", activity_choices)

            unit_choices = sorted(known_units.get(m_activity, {}).values()) or ["unit"]
            mu1, mu2, mu3 = st.columns(3)
            m_unit = mu1.selectbox("Unit", unit_choices)
            m_qty = mu2.number_input("Quantity", value=100.0, step=1.0)
            m_measure = mu3.selectbox("Measurement status", ["measured", "estimated"])

            md1, md2, md3 = st.columns(3)
            m_dq = md1.selectbox("Data quality", ["high", "medium", "low"])
            m_sys = md2.text_input("Source system", value="MANUAL_UI")
            m_prov = md3.text_input("Provenance", value="Manual entry by demo user")

            if st.button("Validate and create draft"):
                manual_df = pd.DataFrame(
                    [
                        {
                            "facility_id": m_fac,
                            "period_start": m_start.isoformat(),
                            "period_end": m_end.isoformat(),
                            "scope": m_scope,
                            "source_category": m_cat,
                            "activity_type": m_activity,
                            "quantity": m_qty,
                            "unit": m_unit,
                            "measurement_status": m_measure,
                            "data_quality": m_dq,
                            "source_system": m_sys,
                            "provenance": m_prov,
                        }
                    ]
                ).astype(str)
                valid_manual, errors_manual, _ = validate_activity_dataframe(manual_df, conn, allow_update=False)
                if not errors_manual[errors_manual["severity"] == "error"].empty:
                    st.error("Manual record has blocking validation errors.")
                    st.dataframe(errors_manual, use_container_width=True)
                elif not valid_manual.empty:
                    inserted, updated = persist_valid_rows(conn, valid_manual, role)
                    audit(
                        conn,
                        role,
                        "manual_record_create",
                        "activity_record",
                        safe_str(valid_manual.iloc[0]["id"]),
                        {"inserted": inserted, "updated": updated},
                    )
                    st.success(f"Created {inserted} draft record.")
                    st.rerun()


def page_review() -> None:
    st.header("Review and approvals")
    role = st.session_state.role

    with db_conn() as conn:
        queue = fetch_df(
            conn,
            """
            SELECT a.id, a.facility_id, f.name AS facility_name, a.period_end, a.scope,
                   a.source_category, a.activity_type, a.quantity, a.unit, a.status,
                   a.exception_flags, a.source_system, a.provenance
            FROM activity_records a
            JOIN facilities f ON f.id=a.facility_id
            WHERE a.status IN ('submitted','exception')
            ORDER BY a.period_end DESC
            """
        )

        if queue.empty:
            st.success("Review queue is empty.")
        else:
            st.dataframe(queue, use_container_width=True, hide_index=True)

            if can(role, "approve") or can(role, "reject"):
                selected = st.selectbox("Select queued record", queue["id"].tolist())
                selected_row = queue[queue["id"] == selected].iloc[0].to_dict()
                rec_base = get_record_dict(conn, selected)
                if rec_base:
                    rec = {**selected_row, **rec_base}
                    st.subheader(f"Review {selected}")
                    st.json(rec)
                    calc, flags = calculate_record_emissions(conn, rec)
                    if calc:
                        st.write(calc["formula"])
                    else:
                        st.warning(f"Calculation blocked: {', '.join(flags)}")

                    r1, r2 = st.columns(2)
                    with r1:
                        if can(role, "approve") and rec.get("status") == "submitted":
                            note = st.text_input("Approval note", key=f"review_approve_{selected}")
                            if st.button("Approve", type="primary"):
                                change_record_status(conn, selected, "approved", role, note, clear_exceptions=True)
                                st.rerun()
                    with r2:
                        if can(role, "reject"):
                            reason = st.text_input("Rejection reason", key=f"review_reject_{selected}")
                            if st.button("Reject / return for correction"):
                                try:
                                    change_record_status(conn, selected, "rejected", role, reason)
                                    st.rerun()
                                except Exception as exc:
                                    st.error(str(exc))

        st.divider()
        st.subheader("Recent audit events")
        audit_df = fetch_df(conn, "SELECT ts, actor_role, action, entity_type, entity_id, details FROM audit_events ORDER BY ts DESC LIMIT 100")
        st.dataframe(audit_df, use_container_width=True, hide_index=True)
        if not audit_df.empty:
            st.download_button(
                "Download audit CSV",
                audit_df.to_csv(index=False).encode("utf-8"),
                file_name="audit_events.csv",
                mime="text/csv",
            )


def page_analysis() -> None:
    st.header("Analysis and reduction planning")
    role = st.session_state.role

    with db_conn() as conn:
        fac_df = fetch_df(conn, "SELECT id, name FROM facilities ORDER BY name")

        f1, f2 = st.columns([2, 1])
        facility_sel = f1.multiselect("Facilities", fac_df["id"].tolist(), default=[])
        include_exceptions = f2.checkbox("Include exception records", value=False)

        d1, d2 = st.columns(2)
        start = d1.date_input("Start date", value=TODAY - timedelta(days=365))
        end = d2.date_input("End date", value=TODAY)

        s1, s2, s3 = st.columns(3)
        use_approved = s1.checkbox("Approved", value=True)
        use_submitted = s2.checkbox("Submitted", value=True)
        use_draft = s3.checkbox("Draft", value=False)

        statuses = []
        if use_approved:
            statuses.append("approved")
        if use_submitted:
            statuses.append("submitted")
        if use_draft:
            statuses.append("draft")
        if not statuses:
            statuses = ["approved"]

        c1, c2, c3, c4 = st.columns(4)
        budget = c1.slider("Budget", min_value=0, max_value=5_000_000, value=1_000_000, step=50_000, format="%d")
        max_months = c2.slider("Max implementation months", min_value=1, max_value=36, value=24)
        min_confidence = c3.slider("Minimum confidence", min_value=0.0, max_value=1.0, value=0.50, step=0.05)
        iterations = c4.slider("Simulation iterations", min_value=50, max_value=2000, value=500, step=50)
        use_overrides = st.checkbox("Use audited manual overrides where present", value=False)

        baseline, detail, exceptions = build_baseline_df(
            conn,
            facility_ids=facility_sel or None,
            start=start,
            end=end,
            statuses=tuple(statuses),
            include_exceptions=include_exceptions,
        )
        options = load_options(conn, facility_ids=facility_sel or None)
        evaluated = evaluate_options(options, baseline, use_overrides=use_overrides)
        portfolio = optimize_portfolio(evaluated, budget=budget, max_months=max_months, min_confidence=min_confidence)

        samples = monte_carlo_baseline(baseline, iterations=iterations, seed=int(st.session_state.get("mc_seed", 12)))
        baseline_mean = float(baseline["tco2e_annual"].sum()) if not baseline.empty else 0.0
        post_samples = simulate_post_samples(samples, portfolio[2], baseline_mean)

        st.subheader("Baseline")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Annual baseline", f"{baseline_mean:,.0f} tCO2e/y")
        k2.metric("Source groups", len(baseline))
        k3.metric("Exceptions", len(exceptions))
        k4.metric("Feasible options", len(portfolio[4]))

        if not baseline.empty:
            st.dataframe(
                baseline[
                    [
                        "facility_name", "scope", "source_category", "activity_type", "unit",
                        "tco2e_annual", "tco2e_annual_low", "tco2e_annual_high",
                        "factor_sources", "avg_uncertainty", "measurement_statuses", "data_qualities"
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

            by_facility = baseline.groupby("facility_name", as_index=False)["tco2e_annual"].sum().sort_values("tco2e_annual", ascending=False)
            fig_fac = px.bar(by_facility, x="facility_name", y="tco2e_annual", title="Annual CO2e by facility", text_auto=".1f")
            st.plotly_chart(fig_fac, use_container_width=True)
        else:
            st.info("No baseline rows for selected filters.")

        if not exceptions.empty:
            st.subheader("Exceptions")
            st.dataframe(exceptions, use_container_width=True, hide_index=True)

        st.subheader("Reduction options")
        if not evaluated.empty:
            show_eval = evaluated[
                [
                    "id", "name", "facility_id", "category", "status", "capex",
                    "opex_savings_annual", "implementation_months", "eligible_baseline_annual",
                    "reduction_annual", "cost_per_tco2e", "confidence", "explanation"
                ]
            ].copy()
            show_eval["cost_per_tco2e"] = show_eval["cost_per_tco2e"].replace([np.inf, -np.inf], np.nan)
            st.dataframe(show_eval, use_container_width=True, hide_index=True)

            scatter = evaluated[evaluated["reduction_annual"] > 0].copy()
            if not scatter.empty:
                scatter["cost_per_tco2e_display"] = scatter["cost_per_tco2e"].replace([np.inf, -np.inf], np.nan)
                fig_scatter = px.scatter(
                    scatter,
                    x="capex",
                    y="reduction_annual",
                    color="cost_per_tco2e_display",
                    size="confidence",
                    hover_name="name",
                    title="Reduction options: capital vs expected reduction",
                    labels={"capex": "Capex", "reduction_annual": "Annual reduction tCO2e", "cost_per_tco2e_display": "Cost per tCO2e"},
                )
                st.plotly_chart(fig_scatter, use_container_width=True)

            selected_ids, spent, reduction, opex, feas = portfolio
            st.subheader("Optimized portfolio")
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Selected options", len(selected_ids))
            p2.metric("Capital", f"{spent:,.0f}")
            p3.metric("Reduction", f"{reduction:,.0f} tCO2e/y")
            p4.metric("Opex savings", f"{opex:,.0f}")

            if selected_ids:
                selected_df = evaluated[evaluated["id"].isin(selected_ids)]
                st.dataframe(
                    selected_df[["id", "name", "capex", "reduction_annual", "cost_per_tco2e", "implementation_months", "explanation"]],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.warning("No feasible portfolio under current constraints.")

            if can(role, "override"):
                st.subheader("Manual override")
                with st.form("override_form"):
                    override_option = st.selectbox("Option", evaluated["id"].tolist())
                    override_value = st.number_input("Override expected annual reduction (tCO2e/y)", value=0.0, step=1.0)
                    override_reason = st.text_input("Reason")
                    submitted = st.form_submit_button("Save audited override")
                    if submitted:
                        try:
                            save_option_override(conn, override_option, override_value, role, override_reason)
                            st.success("Override saved and audited.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
        else:
            st.info("No reduction options available.")

        st.subheader("Uncertainty simulation")
        base_stats = percentile_stats(samples)
        post_stats = percentile_stats(post_samples)
        sim_table = pd.DataFrame(
            [
                {"Scenario": "Baseline", "P10": base_stats["p10"], "P50": base_stats["p50"], "P90": base_stats["p90"], "Mean": base_stats["mean"]},
                {"Scenario": "With portfolio", "P10": post_stats["p10"], "P50": post_stats["p50"], "P90": post_stats["p90"], "Mean": post_stats["mean"]},
            ]
        )
        st.dataframe(sim_table, use_container_width=True, hide_index=True)

        fig_sim = go.Figure()
        fig_sim.add_histogram(x=samples, name="Baseline", opacity=0.55, marker_color="#2c7fb8")
        fig_sim.add_histogram(x=post_samples, name="With portfolio", opacity=0.55, marker_color="#2ca02c")
        fig_sim.update_layout(title="Simulated annual CO2e distribution", xaxis_title="tCO2e/year", yaxis_title="Simulations")
        st.plotly_chart(fig_sim, use_container_width=True)

        st.session_state.analysis_snapshot = {
            "baseline": baseline,
            "detail": detail,
            "exceptions": exceptions,
            "evaluated": evaluated,
            "portfolio": portfolio,
            "filters": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "facility_ids": facility_sel,
                "statuses": statuses,
                "include_exceptions": include_exceptions,
                "budget": budget,
                "max_months": max_months,
                "min_confidence": min_confidence,
            },
        }


def page_reports() -> None:
    st.header("Reports and exports")
    role = st.session_state.role

    snapshot = st.session_state.get("analysis_snapshot")
    with db_conn() as conn:
        if snapshot:
            baseline = snapshot["baseline"]
            detail = snapshot["detail"]
            exceptions = snapshot["exceptions"]
            evaluated = snapshot["evaluated"]
            portfolio = snapshot["portfolio"]
            filters = snapshot["filters"]
        else:
            baseline, detail, exceptions = build_baseline_df(conn, statuses=("approved", "submitted"))
            options = load_options(conn)
            evaluated = evaluate_options(options, baseline, use_overrides=False)
            portfolio = optimize_portfolio(evaluated, budget=1_000_000, max_months=24, min_confidence=0.5)
            filters = {
                "start": (TODAY - timedelta(days=365)).isoformat(),
                "end": TODAY.isoformat(),
                "facility_ids": [],
                "statuses": ["approved", "submitted"],
                "include_exceptions": False,
                "budget": 1_000_000,
                "max_months": 24,
                "min_confidence": 0.5,
            }

        md, html = generate_report(conn, baseline, detail, exceptions, evaluated, portfolio, role, filters)

    st.markdown(md)

    c1, c2, c3, c4 = st.columns(4)
    if not baseline.empty:
        c1.download_button("Download baseline CSV", baseline.to_csv(index=False).encode("utf-8"), file_name="baseline.csv", mime="text/csv")
    if not evaluated.empty:
        c2.download_button("Download options CSV", evaluated.to_csv(index=False).encode("utf-8"), file_name="reduction_options.csv", mime="text/csv")
    if not exceptions.empty:
        c3.download_button("Download exceptions CSV", exceptions.to_csv(index=False).encode("utf-8"), file_name="exceptions.csv", mime="text/csv")
    c4.download_button("Download HTML report", html.encode("utf-8"), file_name="emissions_report.html", mime="text/html")

    st.download_button("Download Markdown report", md.encode("utf-8"), file_name="emissions_report.md", mime="text/markdown")

    with db_conn() as conn:
        audit_df = fetch_df(conn, "SELECT * FROM audit_events ORDER BY ts DESC LIMIT 500")
    if not audit_df.empty:
        st.download_button("Download audit CSV", audit_df.to_csv(index=False).encode("utf-8"), file_name="audit_events.csv", mime="text/csv")


def page_governance() -> None:
    st.header("Governance, data architecture and operations")

    with st.expander("Product brief", expanded=True):
        st.markdown(PRODUCT_BRIEF)

    with st.expander("Functional requirements"):
        st.markdown(FUNCTIONAL_REQUIREMENTS)

    with st.expander("Workflow and roles"):
        st.markdown(
            """
            ### Central workflow
            1. Analyst/operator imports or enters facility activity data.
            2. System validates columns, types, dates, units, duplicates, ranges, and referential integrity.
            3. Invalid rows are rejected with actionable errors; warnings become exception records.
            4. Analyst corrects exceptions and submits records for review.
            5. Reviewer approves, rejects, or returns records with reason.
            6. Approved records feed deterministic emissions calculations using dated factors.
            7. Analyst/reviewer compares reduction options and runs constrained portfolio simulation.
            8. Stakeholder exports decision-support report. Administrator manages backups and reference data.

            ### Role permissions
            - administrator: all actions, archive, override, restore backup, manage reference data.
            - analyst/operator: create, import, edit draft/exception, submit, run analysis, export.
            - reviewer: approve, reject, override with reason, run analysis, export.
            - read-only stakeholder: view, run analysis, export.

            ### Exception path
            Missing, contradictory, late, future-dated, out-of-range, duplicate, or factor-less records are flagged.
            They remain traceable, are excluded from official approved baseline unless explicitly included, and require
            correction or reviewer decision.
            """
        )

    with st.expander("Prioritized backlog"):
        st.markdown(BACKLOG)

    with st.expander("Acceptance checklist"):
        st.markdown(ACCEPTANCE_CHECKLIST)

    with st.expander("Risk register"):
        st.markdown(RISK_REGISTER)

    with st.expander("ERD and data dictionary"):
        st.markdown(ERD_TEXT)
        st.markdown(DATA_DICTIONARY)

    with st.expander("Data lineage and reference versioning"):
        st.markdown(
            """
            - Every activity record stores source system, provenance, schema version, created/updated timestamps.
            - Every import stores filename, SHA-256 checksum, row counts, actor role, and preserved original file path.
            - Every emission factor stores source reference, version, effective_from, effective_to, jurisdiction.
            - Every derived baseline row stores factor IDs, factor sources, formula, uncertainty inputs, and record lineage.
            - Every audit event stores role, action, entity, timestamp, and JSON details.
            - Engine version and schema version are printed in reports.
            """
        )

    with st.expander("Backup, restore, retention and PostgreSQL migration"):
        st.markdown(
            """
            ### Backup
            - Use the backup button to create a SQLite file in `data/backups`.
            - Download the backup and store it outside the application folder.
            - Checksums are stored for imports; database backups should be checksummed by operations.

            ### Restore
            - Stop the app.
            - Replace `emissions_planner.db` with a trusted backup.
            - Restart the app.
            - The admin restore control is present for demo convenience but production restore should be performed by operations.

            ### Retention
            - Original uploaded files are preserved in `data/uploads`.
            - Audit events and imports should be retained according to customer policy.
            - Archived records are soft-retained; deletion workflows belong in team/enterprise edition.

            ### PostgreSQL migration plan
            1. Export SQLite tables to CSV or use `sqlite3 .dump` for migration rehearsal.
            2. Create equivalent PostgreSQL schema with proper types, constraints, and sequences.
            3. Replace SQLite connection with PostgreSQL connection pool.
            4. Move file storage to object storage or managed filesystem with metadata in PostgreSQL.
            5. Add row-level security or application-layer authorization.
            6. Run import, calculation, report, and audit regression tests against PostgreSQL.
            """
        )

    with st.expander("Commercial and deployment tiers"):
        st.markdown(
            """
            - Tier 1: local MVP for one operator, SQLite, synthetic/demo data, manual role selector.
            - Tier 2: team edition with PostgreSQL, authenticated roles, shared approvals, audit exports.
            - Tier 3: enterprise edition with SSO/directory integration, deployment controls, backups, APIs, SLAs, and integration services.

            Pricing hypothesis: per-facility annual subscription plus optional integration/support. Usage-based processing
            can be added for large document or sensor ingestion. Do not promise certification or compliance without
            qualified review and applicable standards.
            """
        )

    if can(st.session_state.role, "restore_backup"):
        st.subheader("Administrator backup controls")
        if st.button("Create SQLite backup now"):
            path = create_sqlite_backup()
            st.success(f"Backup created: {path}")

        backups = list_backups()
        if backups:
            chosen = st.selectbox("Choose backup to download", backups)
            with open(chosen, "rb") as f:
                st.download_button("Download selected backup", f.read(), file_name=os.path.basename(chosen), mime="application/octet-stream")

            restore = st.file_uploader("Restore backup (danger: replaces local database)", type=["db"])
            confirm = st.checkbox("I confirm I want to replace the local database")
            if restore and confirm:
                if st.button("Restore now"):
                    create_sqlite_backup()
                    with open(DB_PATH, "wb") as f:
                        f.write(restore.getvalue())
                    st.cache_resource.clear()
                    st.success("Database restored. Reloading...")
                    st.rerun()
    else:
        st.info("Backup restore controls are available to administrator role only.")


def page_qa() -> None:
    st.header("QA and automated checks")
    st.caption("These tests check software plumbing and deterministic domain behaviour. They do not prove real-world regulatory validity.")

    if st.button("Run full self-test suite", type="primary"):
        with st.spinner("Running tests..."):
            df = run_self_tests()
        st.session_state.qa_results = df

    df = st.session_state.get("qa_results")
    if df is not None:
        st.dataframe(df, use_container_width=True, hide_index=True)
        passed = int(df["pass"].sum())
        total = len(df)
        st.metric("Tests passed", f"{passed}/{total}")
        if passed == total:
            st.success("All automated checks passed.")
        else:
            st.error("Some checks failed. Review messages above.")
    else:
        st.info("Press the button to run the test suite.")

    st.subheader("Reproducible commands")
    st.code(
        """# From Windows PowerShell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
streamlit run app.py

# Optional headless test run
$env:RUN_TESTS="1"
python app.py
""",
        language="powershell",
    )


def main() -> None:
    bootstrap_demo()

    if "role" not in st.session_state:
        st.session_state.role = "analyst/operator"
    if "mc_seed" not in st.session_state:
        st.session_state.mc_seed = 12
    if "mc_iterations" not in st.session_state:
        st.session_state.mc_iterations = 300

    st.sidebar.title(APP_NAME)
    st.sidebar.caption("Local MVP · SQLite · synthetic demo data · no paid API keys")

    try:
        role_index = ROLES.index(st.session_state.role)
    except ValueError:
        role_index = 0
    st.session_state.role = st.sidebar.selectbox("Demo role (not authenticated)", ROLES, index=role_index)

    page = st.sidebar.radio(
        "Workspace",
        [
            "Live Demo",
            "Import",
            "Records",
            "Review & approvals",
            "Analysis",
            "Reports",
            "Governance",
            "QA & tests",
        ],
    )

    if st.sidebar.button("Reset demo database"):
        try:
            bootstrap_demo()
            if os.path.exists(DB_PATH):
                backup = DB_PATH + ".reset_backup"
                os.replace(DB_PATH, backup)
            st.cache_resource.clear()
            st.sidebar.success("Demo database reset. Reloading...")
            st.rerun()
        except Exception as exc:
            st.sidebar.error(f"Reset failed: {exc}")

    st.sidebar.divider()
    st.sidebar.caption(f"Engine {ENGINE_VERSION} · Schema {SCHEMA_VERSION}")

    if page == "Live Demo":
        page_live_demo()
    elif page == "Import":
        page_import()
    elif page == "Records":
        page_records()
    elif page == "Review & approvals":
        page_review()
    elif page == "Analysis":
        page_analysis()
    elif page == "Reports":
        page_reports()
    elif page == "Governance":
        page_governance()
    elif page == "QA & tests":
        page_qa()


if os.environ.get("RUN_TESTS") == "1":
    run_tests_headless()
else:
    main()