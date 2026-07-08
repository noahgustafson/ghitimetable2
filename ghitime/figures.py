"""Figure derivations. Every value produced here is CALCULATED and must be
rendered alongside its tag; a None means BLANK + visibly flagged — never a
default, never an invention (spec figure rules).

All arithmetic is Decimal; money stays in integer cents.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

CALCULATED = "CALCULATED"
SOURCE = "SOURCE"

# workweek_start_dow config semantics: 0=Monday … 6=Sunday (ISO weekday - 1).
DISPLAY_WEEK_START = 0  # Mon–Sun display grouping (operator-confirmed); OT
# figures additionally require the config value to be SET — display grouping
# is presentation only and never feeds an OT figure.


def week_start(d: date, start_dow: int) -> date:
    return d - timedelta(days=(d.weekday() - start_dow) % 7)


def rate_cents_as_of(conn: sqlite3.Connection, table: str, person_id: int, on: str) -> int | None:
    """Rate in force on a date, or None (blank + flagged upstream)."""
    assert table in ("rate_pay", "rate_bill")
    row = conn.execute(
        f"SELECT hourly_rate_cents FROM v_{table}_effective"
        " WHERE person_id=? AND effective_date<=?"
        " ORDER BY effective_date DESC LIMIT 1",
        (person_id, on),
    ).fetchone()
    return row["hourly_rate_cents"] if row else None


@dataclass(frozen=True)
class OtPolicy:
    threshold_hours: Decimal
    multiplier: Decimal
    effective_date: str


def ot_policy_in_force(conn: sqlite3.Connection, on: str) -> OtPolicy | None:
    """Policy in force on a date = greatest effective_date <= that date.
    None => no policy in force: OT figures render blank + flagged."""
    row = conn.execute(
        "SELECT threshold_hours, multiplier, effective_date FROM v_ot_policy_effective"
        " WHERE effective_date<=? ORDER BY effective_date DESC LIMIT 1",
        (on,),
    ).fetchone()
    if row is None:
        return None
    return OtPolicy(
        threshold_hours=Decimal(str(row["threshold_hours"])),
        multiplier=Decimal(str(row["multiplier"])),
        effective_date=row["effective_date"],
    )


def minutes_to_hours(minutes: int | None) -> Decimal | None:
    if minutes is None:
        return None
    return (Decimal(minutes) / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def gross_preview_cents(minutes: int | None, rate_cents: int | None) -> int | None:
    """hours x rate, CALCULATED preview — actual pay is the bookkeeper's."""
    if minutes is None or rate_cents is None:
        return None
    return int(
        (Decimal(rate_cents) * Decimal(minutes) / 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def ot_hours_past_threshold(week_minutes: int, policy: OtPolicy | None) -> Decimal | None:
    """Weekly OT hours under the policy in force for that week, or None."""
    if policy is None:
        return None
    hours = Decimal(week_minutes) / 60
    ot = hours - policy.threshold_hours
    if ot <= 0:
        return Decimal("0.00")
    return ot.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cents_to_str(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"{Decimal(cents) / 100:.2f}"


def weekly_minutes(
    conn: sqlite3.Connection,
    person_id: int,
    wk_start: date,
    statuses: tuple[str, ...] = ("draft", "submitted", "approved"),
) -> tuple[int, int]:
    """(summed worked minutes, count of entries with blank duration) for the
    week starting wk_start. void is ALWAYS excluded (Gate 2 binding #2);
    blank durations are counted, never guessed."""
    wk_end = wk_start + timedelta(days=6)
    qmarks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT worked_minutes FROM v_time_entry_minutes"
        f" WHERE person_id=? AND work_date>=? AND work_date<=? AND status IN ({qmarks})",
        (person_id, wk_start.isoformat(), wk_end.isoformat(), *statuses),
    ).fetchall()
    total = sum(r["worked_minutes"] for r in rows if r["worked_minutes"] is not None)
    blanks = sum(1 for r in rows if r["worked_minutes"] is None)
    return total, blanks
