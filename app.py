"""Studypilot — Student Assignment Planner & To-Do Application.

Comprehensive workflow integrating SQLite database operations, NumPy/Matplotlib
circular progress visualisations, automated Gmail reminders & reports, and an
interactive Streamlit dashboard.
"""

from __future__ import annotations

import os
import sqlite3
import smtplib
import sys
import types
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Wedge
import streamlit as st

# =========================================================================== #
# SECTION 1: DATABASE & CONFIGURATION (db)
# =========================================================================== #


APP_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "studypilot.db"

PRIORITIES = ("High", "Medium", "Low")

# A priority decides *how early* the reminder fires, not just how it looks.
PRIORITY_RULES: dict[str, dict] = {
    "High": {"emoji": "🔴", "remind_days_before": 3, "hint": "remind 3 days early"},
    "Medium": {"emoji": "🟠", "remind_days_before": 2, "hint": "remind 2 days early"},
    "Low": {"emoji": "🟢", "remind_days_before": 1, "hint": "remind 1 day early"},
}

REMINDER_COOLDOWN_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    course           TEXT    NOT NULL DEFAULT '',
    due_at           TEXT    NOT NULL,
    priority         TEXT    NOT NULL DEFAULT 'Medium',
    notes            TEXT    NOT NULL DEFAULT '',
    is_done          INTEGER NOT NULL DEFAULT 0,
    completed_at     TEXT,
    created_at       TEXT    NOT NULL,
    last_reminder_at TEXT
);

CREATE TABLE IF NOT EXISTS profile (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    name           TEXT    NOT NULL DEFAULT '',
    email          TEXT    NOT NULL DEFAULT '',
    auto_reminders INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_assignments_due ON assignments (due_at);
"""

EMPTY_COLUMNS = [
    "id", "title", "course", "due_at", "priority", "notes", "is_done",
    "completed_at", "created_at", "last_reminder_at", "status", "days_left",
]


@contextmanager
def connect():
    """Open a short-lived connection with row access by name."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Initialize database tables and default profile."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO profile (id, name, email, auto_reminders) "
            "VALUES (1, '', '', 1)"
        )


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
def get_profile() -> dict:
    """Fetch student profile settings."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if row is None:
        return {"id": 1, "name": "", "email": "", "auto_reminders": 1}
    return dict(row)


def save_profile(name: str, email: str, auto_reminders: bool) -> None:
    """Save or update student profile settings."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO profile (id, name, email, auto_reminders) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name = excluded.name, email = excluded.email, auto_reminders = excluded.auto_reminders",
            (name.strip(), email.strip(), 1 if auto_reminders else 0),
        )


# --------------------------------------------------------------------------- #
# Assignments
# --------------------------------------------------------------------------- #
def add_assignment(
    title: str,
    course: str,
    due_at: datetime | date,
    priority: str,
    notes: str = "",
) -> int:
    """Add a new assignment and return its autoincrement ID."""
    if isinstance(due_at, date) and not isinstance(due_at, datetime):
        due_at = datetime.combine(due_at, time(23, 59))
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO assignments "
            "(title, course, due_at, priority, notes, is_done, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                title.strip(),
                course.strip(),
                due_at.replace(second=0, microsecond=0).isoformat(timespec="minutes"),
                priority if priority in PRIORITIES else "Medium",
                notes.strip(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid or 0)


def set_done(assignment_id: int, done: bool) -> None:
    """Mark an assignment completed or pending."""
    with connect() as conn:
        conn.execute(
            "UPDATE assignments SET is_done = ?, completed_at = ? WHERE id = ?",
            (
                1 if done else 0,
                datetime.now().isoformat(timespec="seconds") if done else None,
                int(assignment_id),
            ),
        )


def delete_assignment(assignment_id: int) -> None:
    """Delete an assignment by its ID."""
    with connect() as conn:
        conn.execute("DELETE FROM assignments WHERE id = ?", (int(assignment_id),))


def mark_reminded(assignment_ids: list[int]) -> None:
    """Update last_reminder_at timestamp for a batch of assignments."""
    if not assignment_ids:
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.executemany(
            "UPDATE assignments SET last_reminder_at = ? WHERE id = ?",
            [(stamp, int(i)) for i in assignment_ids],
        )


def fetch_assignments() -> pd.DataFrame:
    """Return every assignment with derived ``status`` / ``days_left`` columns."""
    with connect() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM assignments ORDER BY is_done ASC, due_at ASC", conn
        )

    if df.empty:
        empty_df = pd.DataFrame(columns=EMPTY_COLUMNS)
        empty_df["due_at"] = pd.to_datetime(empty_df["due_at"])
        empty_df["completed_at"] = pd.to_datetime(empty_df["completed_at"])
        empty_df["created_at"] = pd.to_datetime(empty_df["created_at"])
        empty_df["last_reminder_at"] = pd.to_datetime(empty_df["last_reminder_at"])
        empty_df["days_left"] = pd.to_numeric(empty_df["days_left"])
        return empty_df

    now = pd.Timestamp.now()
    df["due_at"] = pd.to_datetime(df["due_at"])
    df["completed_at"] = pd.to_datetime(df["completed_at"], errors="coerce")
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["last_reminder_at"] = pd.to_datetime(df["last_reminder_at"], errors="coerce")
    df["days_left"] = (df["due_at"] - now).dt.total_seconds() / 86_400

    df["status"] = "Pending"
    df.loc[df["due_at"] < now, "status"] = "Overdue"
    df.loc[df["is_done"] == 1, "status"] = "Completed"
    return df


# --------------------------------------------------------------------------- #
# Reminder selection
# --------------------------------------------------------------------------- #
def needs_reminder(row: pd.Series | dict, now: datetime | None = None) -> bool:
    """True when an assignment falls inside its priority reminder window."""
    if int(row["is_done"]) == 1:
        return False
    now = now or datetime.now()
    due = row["due_at"]
    if hasattr(due, "to_pydatetime"):
        due_dt = due.to_pydatetime()
    elif isinstance(due, datetime):
        due_dt = due
    else:
        due_dt = pd.to_datetime(due).to_pydatetime()

    if due_dt.tzinfo is not None:
        due_dt = due_dt.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    hours_left = (due_dt - now).total_seconds() / 3600
    window = PRIORITY_RULES.get(row["priority"], PRIORITY_RULES["Medium"])[
        "remind_days_before"
    ] * 24
    return hours_left <= window


def reminder_cooldown_over(row: pd.Series | dict, now: datetime | None = None) -> bool:
    """Avoid emailing the same assignment repeatedly within the cooldown."""
    last = row.get("last_reminder_at")
    if last is None or pd.isna(last):
        return True
    now = now or datetime.now()
    if hasattr(last, "to_pydatetime"):
        last_dt = last.to_pydatetime()
    elif isinstance(last, datetime):
        last_dt = last
    else:
        last_dt = pd.to_datetime(last).to_pydatetime()

    if last_dt.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    return (now - last_dt) >= timedelta(hours=REMINDER_COOLDOWN_HOURS)


def reminders_to_send(df: pd.DataFrame) -> pd.DataFrame:
    """Filter assignments that are ready to trigger an email notification."""
    if df.empty:
        return df
    now = datetime.now()
    mask = df.apply(lambda r: needs_reminder(r, now) and reminder_cooldown_over(r, now), axis=1)
    return df[mask]


# --------------------------------------------------------------------------- #
# Summary / analytics
# --------------------------------------------------------------------------- #
def summarise(df: pd.DataFrame) -> dict:
    """Aggregate the numbers the summary tab, charts and report all rely on."""
    now = pd.Timestamp.now()
    today = date.today()
    week_start = pd.Timestamp(today - timedelta(days=today.weekday()))
    week_end = week_start + pd.Timedelta(days=7)
    last_week_start = week_start - pd.Timedelta(days=7)

    total = int(len(df))
    if total == 0:
        return {
            "total": 0, "completed": 0, "pending": 0, "overdue": 0,
            "completion_rate": 0.0, "on_time_rate": 0.0,
            "completed_this_week": 0, "completed_last_week": 0,
            "due_this_week": 0, "week_start": week_start, "week_end": week_end,
            "days": [], "day_counts": [], "by_priority": {p: 0 for p in PRIORITIES},
            "by_course": pd.DataFrame(columns=["Course", "total", "completed"]),
            "overdue_list": [],
        }

    done = df[df["is_done"] == 1]
    pending = df[df["is_done"] == 0]
    overdue = pending[pending["due_at"] < now]

    completed_this_week = int(
        ((done["completed_at"] >= week_start) & (done["completed_at"] < week_end)).sum()
    )
    completed_last_week = int(
        ((done["completed_at"] >= last_week_start) & (done["completed_at"] < week_start)).sum()
    )
    due_this_week = int(
        (
            (pending["due_at"] >= week_start)
            & (pending["due_at"] < week_end)
        ).sum()
    )
    on_time = int((done["completed_at"] <= done["due_at"]).sum()) if len(done) else 0

    # Last seven days of completions, oldest first.
    days: list[str] = []
    day_counts: list[int] = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        start = pd.Timestamp(day)
        end = start + pd.Timedelta(days=1)
        days.append(day.strftime("%a"))
        day_counts.append(int(((done["completed_at"] >= start) & (done["completed_at"] < end)).sum()))

    by_priority = {
        p: int((pending["priority"] == p).sum()) for p in PRIORITIES
    }
    by_course = (
        df.groupby("course")
        .agg(total=("id", "count"), completed=("is_done", "sum"))
        .reset_index()
        .rename(columns={"course": "Course"})
    )

    return {
        "total": total,
        "completed": int(len(done)),
        "pending": int(len(pending)),
        "overdue": int(len(overdue)),
        "completion_rate": round(100 * len(done) / total, 1),
        "on_time_rate": round(100 * on_time / len(done), 1) if len(done) else 0.0,
        "completed_this_week": completed_this_week,
        "completed_last_week": completed_last_week,
        "due_this_week": due_this_week,
        "week_start": week_start,
        "week_end": week_end,
        "days": days,
        "day_counts": day_counts,
        "by_priority": by_priority,
        "by_course": by_course,
        "overdue_list": overdue.sort_values("due_at").to_dict("records"),
    }


def seed_demo_data() -> int:
    """Insert a small, realistic set of assignments (used by the demo button)."""
    now = datetime.now().replace(second=0, microsecond=0)
    samples = [
        ("Linear algebra problem set 4", "MATH 201", now - timedelta(days=1, hours=3), "High", "Chapter 5, exercises 1-18"),
        ("Essay: Industrial revolution", "HIST 110", now + timedelta(hours=20), "High", "1200 words, cite two sources"),
        ("Lab report: titration", "CHEM 105", now + timedelta(days=2), "Medium", "Include error analysis"),
        ("Reading response", "ENGL 120", now + timedelta(days=4), "Low", "One page, no citations needed"),
        ("Group project slides", "CS 210", now + timedelta(days=6), "High", "Slides 8-14 are mine"),
        ("Practice quiz", "STAT 150", now + timedelta(days=8), "Low", ""),
    ]
    ids = [add_assignment(t, c, due, p, n) for t, c, due, p, n in samples]
    # A couple of things already finished this week, for the weekly summary.
    for assignment_id, days_ago in ((ids[3], 0), (ids[5], 3)):
        set_done(assignment_id, True)
        with connect() as conn:
            conn.execute(
                "UPDATE assignments SET completed_at = ? WHERE id = ?",
                (
                    (now - timedelta(days=days_ago, hours=2)).isoformat(timespec="seconds"),
                    assignment_id,
                ),
            )
    return len(ids)

# =========================================================================== #
# SECTION 2: CHARTS & VISUALISATIONS (charts)
# =========================================================================== #


TEXT = "#1F2430"
MUTED = "#6B7280"
TRACK = "#E8EAF3"

STATUS_COLORS = {
    "Completed": "#22C55E",
    "Pending": "#6366F1",
    "Overdue": "#EF4444",
}

RING_GRADIENT = (np.array([199, 210, 254]), np.array([67, 56, 202]))  # light -> deep indigo


def completion_donut(counts: dict[str, int], center_label: str = "complete") -> plt.Figure:
    """Donut chart of completed / pending / overdue assignments."""
    labels = ["Completed", "Pending", "Overdue"]
    values = np.array([max(int(counts.get(label, 0)), 0) for label in labels], dtype=float)
    total = values.sum()

    fig = plt.figure(figsize=(3.8, 4.15), dpi=200)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0.02, 0.16, 0.96, 0.82], aspect="equal")
    ax.set_axis_off()

    outer, inner = 1.0, 0.64

    if total == 0:
        ax.add_patch(
            Wedge((0, 0), outer, 0, 360, width=outer - inner, facecolor=TRACK, edgecolor="none")
        )
    else:
        fractions = values / total
        edges = np.concatenate(([0.0], np.cumsum(fractions)))
        start_angle = 90.0
        for index, label in enumerate(labels):
            sweep = 360.0 * float(fractions[index])
            if sweep <= 0:
                continue
            gap = min(1.4, sweep * 0.12)  # small breathing room between arcs
            theta_hi = start_angle - 360.0 * float(edges[index + 1])
            theta_lo = start_angle - 360.0 * float(edges[index])
            ax.add_patch(
                Wedge(
                    (0, 0),
                    outer,
                    theta_hi + gap,
                    theta_lo - gap,
                    width=outer - inner,
                    facecolor=STATUS_COLORS[label],
                    edgecolor="none",
                )
            )

    percent = 0.0 if total == 0 else 100.0 * values[0] / total
    ax.text(0, 0.06, f"{percent:.0f}%", ha="center", va="center",
            fontsize=27, fontweight="bold", color=TEXT)
    ax.text(0, -0.19, center_label, ha="center", va="center", fontsize=9, color=MUTED)
    ax.set_xlim(-1.1, 1.1)
    ax.set_ylim(-1.08, 1.08)

    handles = [
        Line2D([], [], marker="o", linestyle="", markersize=8,
               markerfacecolor=STATUS_COLORS[label], markeredgecolor="none",
               label=f"{label} {int(values[i])}")
        for i, label in enumerate(labels)
    ]
    fig.legend(
        handles=handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=3,
        frameon=False,
        fontsize=9,
        handletextpad=0.35,
        columnspacing=1.2,
        labelcolor=TEXT,
    )
    return fig


def weekly_radial(day_labels: list[str], values: list[int]) -> plt.Figure:
    """Circular bar chart: one tall bar per day of the last week."""
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        values = np.zeros(7)
        day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    days = len(values)
    theta = np.arange(days) * 2 * np.pi / days
    width = (2 * np.pi / days) * 0.66
    inner = 0.30  # keep a hole so the ring reads as a ring, not a pie
    track_top = 0.98
    bar_top = 0.86
    peak = float(values.max())

    fig = plt.figure(figsize=(4.2, 4.2), dpi=200)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0.09, 0.09, 0.82, 0.82], projection="polar")
    ax.set_facecolor("none")
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)

    # Background track for every day, so empty days still read as "a day".
    ax.bar(theta, np.full(days, track_top - inner), width=width, bottom=inner,
           color=TRACK, edgecolor="none", zorder=1)

    if peak > 0:
        low, high = RING_GRADIENT
        norm = values / peak
        colors = (low + norm[:, None] * (high - low)) / 255.0
        heights = norm * (bar_top - inner - 0.04)
        ax.bar(theta, heights, width=width, bottom=inner,
               color=colors, edgecolor="none", zorder=2)
        ax.text(0.5, 0.55, f"{int(values.sum())}", ha="center", va="center",
                fontsize=21, fontweight="bold", color=TEXT, transform=ax.transAxes, zorder=3)
        ax.text(0.5, 0.43, "this week", ha="center", va="center",
                fontsize=8, color=MUTED, transform=ax.transAxes, zorder=3)
    else:
        ax.text(0.5, 0.5, "no\ncompletions\nyet", ha="center", va="center",
                fontsize=9, color=MUTED, transform=ax.transAxes, zorder=3)

    ax.set_xticks(theta)
    ax.set_xticklabels(day_labels, fontsize=9, color=MUTED)
    ax.tick_params(axis="x", pad=6, length=0)
    ax.set_ylim(0, 1.0)
    ax.set_yticks([])
    ax.spines["polar"].set_visible(False)
    ax.grid(False)

    return fig

# =========================================================================== #
# SECTION 3: EMAIL DELIVERY & NOTIFICATIONS (mailer)
# =========================================================================== #


SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def send_email(
    smtp_user: str,
    app_password: str,
    to_addr: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> tuple[bool, str]:
    """Send one email through Gmail. Returns ``(ok, message)``."""
    if not smtp_user or not app_password:
        return False, "Add your Gmail address and app password in the sidebar first."
    if not to_addr:
        return False, "Add a notification email to your profile in the sidebar."

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Studypilot Reminders", smtp_user))
    message["To"] = to_addr
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=25) as server:
            server.login(smtp_user, app_password.replace(" ", ""))
            server.send_message(message)
        return True, f"Email sent to {to_addr}"
    except smtplib.SMTPAuthenticationError:
        return False, (
            "Gmail rejected that login. Turn on 2-Step Verification, then create a "
            "16-character App Password (myaccount.google.com → Security → App passwords) "
            "and paste it here — a normal Gmail password will not work."
        )
    except smtplib.SMTPRecipientsRefused:
        return False, f"Gmail refused the recipient address {to_addr}."
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"Could not reach Gmail: {exc}"
    except Exception as exc:
        return False, f"An unexpected error occurred while sending email: {exc}"


# --------------------------------------------------------------------------- #
# Message building
# --------------------------------------------------------------------------- #
def _when(due_at: datetime, days_left: float) -> str:
    if hasattr(due_at, "to_pydatetime"):
        due_at = due_at.to_pydatetime()
    stamp = due_at.strftime("%a %d %b %Y, %H:%M")
    if days_left < -0.04:
        late = abs(days_left)
        if late < 1:
            return f"{stamp} · overdue by {max(1, round(late * 24))}h"
        return f"{stamp} · overdue by {round(late)} day(s)"
    if days_left < 1:
        return f"{stamp} · due in {max(1, round(days_left * 24))}h"
    return f"{stamp} · due in {round(days_left)} day(s)"


def _html_table(assignments: list[dict]) -> str:
    rows = []
    for item in assignments:
        priority = item["priority"]
        rule = db.PRIORITY_RULES.get(priority, db.PRIORITY_RULES["Medium"])
        rows.append(
            "<tr>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #EDEFF5;'>"
            f"<strong style='color:#1F2430'>{escape(item['title'])}</strong>"
            f"<br><span style='color:#6B7280;font-size:12px'>{escape(item.get('course') or 'General')}</span>"
            "</td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #EDEFF5;color:#6B7280;font-size:12px'>"
            f"{escape(_when(item['due_at'], item['days_left']))}</td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #EDEFF5;font-size:12px;"
            f"color:#374151;white-space:nowrap'>{rule['emoji']} {escape(priority)}</td>"
            "</tr>"
        )
    return (
        "<table style='border-collapse:collapse;width:100%;font-family:Arial,Helvetica,sans-serif;"
        "font-size:14px;background:#fff;border:1px solid #EDEFF5;border-radius:10px'>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _wrap(title: str, intro: str, body_html: str, footer: str) -> str:
    return f"""\
<div style="background:#F5F6FB;padding:24px;font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:620px;margin:0 auto">
    <div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#6366F1;font-weight:700">
      Studypilot
    </div>
    <h2 style="color:#1F2430;margin:6px 0 4px;font-size:22px">{escape(title)}</h2>
    <p style="color:#4B5563;margin:0 0 16px;font-size:14px;line-height:1.5">{intro}</p>
    {body_html}
    <p style="color:#9CA3AF;font-size:12px;margin-top:18px;line-height:1.5">{footer}</p>
  </div>
</div>"""


def build_reminder(student_name: str, assignments: list[dict]) -> tuple[str, str, str]:
    """Subject, plain text and HTML for an assignment reminder."""
    who = student_name.strip() or "there"
    if len(assignments) == 1:
        subject = f"⏰ Reminder: {assignments[0]['title']} is due soon"
    else:
        subject = f"⏰ {len(assignments)} assignments need your attention"

    lines = [f"Hi {who},", "", "Here is what needs you next:", ""]
    for item in assignments:
        lines.append(
            f"- [{item['priority']}] {item['title']} ({item.get('course') or 'General'}) — "
            f"{_when(item['due_at'], item['days_left'])}"
        )
    lines += ["", "Tick them off in Studypilot once they are done.", "", "— Studypilot"]

    html = _wrap(
        "Assignments due soon",
        f"Hi {escape(who)}, these tasks are inside their reminder window.",
        _html_table(assignments),
        "You picked the priority for each task — Studypilot reminds you 1–3 days ahead based on it.",
    )
    return subject, "\n".join(lines), html


def build_report(
    student_name: str,
    summary: dict,
    assignments: list[dict],
) -> tuple[str, str, str]:
    """Subject, plain text and HTML for the weekly progress report."""
    who = student_name.strip() or "there"
    subject = (
        f"📊 Weekly progress: {summary['completed_this_week']} completed, "
        f"{summary['overdue']} overdue"
    )
    stats = [
        ("Overall completion", f"{summary['completion_rate']}%"),
        ("Completed this week", summary["completed_this_week"]),
        ("Completed last week", summary["completed_last_week"]),
        ("Due this week", summary["due_this_week"]),
        ("Overdue", summary["overdue"]),
        ("On-time rate", f"{summary['on_time_rate']}%"),
    ]
    text_lines = [f"Hi {who},", "", "Your week in Studypilot:", ""]
    text_lines += [f"• {label}: {value}" for label, value in stats]
    text_lines += ["", "Open tasks:", ""]
    for item in assignments:
        text_lines.append(
            f"- {item['title']} ({item.get('course') or 'General'}) — "
            f"{_when(item['due_at'], item['days_left'])}"
        )
    text_lines += ["", "— Studypilot"]

    stat_html = "".join(
        f"<td style='padding:10px 14px;text-align:center'>"
        f"<div style='font-size:20px;font-weight:700;color:#1F2430'>{escape(str(value))}</div>"
        f"<div style='font-size:11px;color:#6B7280'>{escape(label)}</div></td>"
        for label, value in stats
    )
    body = (
        "<table style='border-collapse:collapse;width:100%;background:#fff;"
        "border:1px solid #EDEFF5;border-radius:10px;font-family:Arial,Helvetica,sans-serif'>"
        f"<tr>{stat_html}</tr></table>"
        "<h3 style='color:#1F2430;font-size:15px;margin:18px 0 8px'>Still open</h3>"
        + _html_table(assignments)
    )
    html = _wrap("Your weekly progress report", f"Hi {escape(who)}, here is where things stand.", body,
                 "Download the full CSV any time from the Progress tab in Studypilot.")
    return subject, "\n".join(text_lines), html

# =========================================================================== #
# SECTION 4: COMPATIBILITY NAMESPACES
# =========================================================================== #

class _ModuleNamespace:
    """Namespace holder so that db.func, charts.func, mailer.func work inside workflow.py."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

db = _ModuleNamespace(
    APP_DIR=APP_DIR,
    DATA_DIR=DATA_DIR,
    DB_PATH=DB_PATH,
    PRIORITIES=PRIORITIES,
    PRIORITY_RULES=PRIORITY_RULES,
    REMINDER_COOLDOWN_HOURS=REMINDER_COOLDOWN_HOURS,
    SCHEMA=SCHEMA,
    EMPTY_COLUMNS=EMPTY_COLUMNS,
    connect=connect,
    init_db=init_db,
    get_profile=get_profile,
    save_profile=save_profile,
    add_assignment=add_assignment,
    set_done=set_done,
    delete_assignment=delete_assignment,
    mark_reminded=mark_reminded,
    fetch_assignments=fetch_assignments,
    needs_reminder=needs_reminder,
    reminder_cooldown_over=reminder_cooldown_over,
    reminders_to_send=reminders_to_send,
    summarise=summarise,
    seed_demo_data=seed_demo_data,
)

charts = _ModuleNamespace(
    TEXT=TEXT,
    MUTED=MUTED,
    TRACK=TRACK,
    STATUS_COLORS=STATUS_COLORS,
    RING_GRADIENT=RING_GRADIENT,
    completion_donut=completion_donut,
    weekly_radial=weekly_radial,
)

mailer = _ModuleNamespace(
    SMTP_HOST=SMTP_HOST,
    SMTP_PORT=SMTP_PORT,
    send_email=send_email,
    _when=_when,
    _html_table=_html_table,
    _wrap=_wrap,
    build_reminder=build_reminder,
    build_report=build_report,
)

sys.modules.setdefault("db", sys.modules[__name__])
sys.modules.setdefault("charts", sys.modules[__name__])
sys.modules.setdefault("mailer", sys.modules[__name__])

# =========================================================================== #
# SECTION 5: STREAMLIT DASHBOARD & APPLICATION LOGIC
# =========================================================================== #


st.set_page_config(
    page_title="Studypilot · Assignment To-Do",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

PRIORITY_OPTIONS = list(db.PRIORITIES)
STATUS_OPTIONS = ["Pending", "Overdue", "Completed"]

CSS = """
<style>
  .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1180px; }
  .hero { display:flex; align-items:center; justify-content:space-between; gap:18px;
          flex-wrap:wrap; margin-bottom: 6px; }
  .hero h1 { font-size: 1.9rem; margin: 0; letter-spacing:-0.02em; }
  .hero p { margin: 2px 0 0; color:#6B7280; font-size:.95rem; }
  .chip { display:inline-flex; align-items:center; gap:6px; padding:6px 12px;
          border-radius:999px; background:#EEF0FF; color:#3F3FBF;
          font-size:.82rem; font-weight:600; }
  .pill { display:inline-block; padding:2px 9px; border-radius:999px;
          font-size:.7rem; font-weight:700; letter-spacing:.02em;
          vertical-align: middle; margin-right:6px; }
  .pill-high { background:#FEE2E2; color:#B91C1C; }
  .pill-medium { background:#FEF3C7; color:#B45309; }
  .pill-low { background:#DCFCE7; color:#15803D; }
  .pill-course { background:#EEF2FF; color:#4338CA; }
  .pill-due { background:#F3F4F6; color:#4B5563; }
  .pill-late { background:#FEE2E2; color:#B91C1C; }
  .pill-done { background:#DCFCE7; color:#15803D; }
  .task-title { font-size:1.02rem; font-weight:600; color:#111827; }
  .task-title.done { color:#9CA3AF; text-decoration: line-through; }
  .task-notes { color:#6B7280; font-size:.83rem; margin-top:4px; }
  .section-note { color:#6B7280; font-size:.88rem; }
  div[data-testid="stMetricValue"] { font-size:1.55rem; }
  div[data-testid="stMetric"] { background:#F8F9FD; border:1px solid #EDEFF5;
                                border-radius:14px; padding:12px 14px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #
db.init_db()


def flash(message: str, kind: str = "success") -> None:
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    pending = st.session_state.pop("_flash", None)
    if pending:
        kind, message = pending
        getattr(st, kind)(message)


def secret_value(section: str, key: str) -> str:
    try:
        return str(st.secrets[section][key])
    except Exception:
        return ""


profile = db.get_profile()
st.session_state.setdefault(
    "gmail_user", secret_value("gmail", "address") or os.environ.get("GMAIL_ADDRESS", "")
)
st.session_state.setdefault(
    "gmail_pass",
    secret_value("gmail", "app_password") or os.environ.get("GMAIL_APP_PASSWORD", ""),
)


def priority_pill(priority: str) -> str:
    rule = db.PRIORITY_RULES.get(priority, db.PRIORITY_RULES["Medium"])
    return f"<span class='pill pill-{priority.lower()}'>{rule['emoji']} {priority}</span>"


def human_when(row) -> tuple[str, str]:
    """Return (text, css class) describing the distance to the due date."""
    if int(row["is_done"]) == 1:
        return "✅ completed", "pill-done"
    hours = float(row["days_left"]) * 24
    if hours < -0.5:
        late = abs(hours)
        text = f"overdue by {round(late / 24)}d" if late >= 24 else f"overdue by {round(late)}h"
        return text, "pill-late"
    if hours < 24:
        return f"due in {max(1, round(hours))}h", "pill-due"
    return f"due in {round(hours / 24)}d", "pill-due"


def send_reminder(rows: list[dict]) -> tuple[bool, str]:
    assignments = [
        {
            "title": row["title"],
            "course": row["course"],
            "due_at": row["due_at"].to_pydatetime() if hasattr(row["due_at"], "to_pydatetime") else row["due_at"],
            "priority": row["priority"],
            "notes": row["notes"],
            "days_left": float(row["days_left"]),
        }
        for row in rows
    ]
    subject, text, html = mailer.build_reminder(profile["name"], assignments)
    return mailer.send_email(
        st.session_state["gmail_user"],
        st.session_state["gmail_pass"],
        profile["email"],
        subject,
        text,
        html,
    )


def weekly_narrative(summary: dict) -> str:
    if summary["total"] == 0:
        return "Add your first assignment to start tracking progress."

    start = summary["week_start"].strftime("%d %b")
    end = (summary["week_end"] - pd.Timedelta(days=1)).strftime("%d %b")
    done_now = summary["completed_this_week"]
    done_before = summary["completed_last_week"]

    if done_now > done_before:
        trend = f"up from {done_before} last week 📈"
    elif done_now < done_before:
        trend = f"down from {done_before} last week 📉"
    else:
        trend = f"the same as last week ({done_before})"

    lines = [
        f"**Week {start} – {end}:** you completed **{done_now} assignment"
        f"{'' if done_now == 1 else 's'}** ({trend}).",
        f"**{summary['due_this_week']}** still due before the week ends, and "
        f"**{summary['overdue']}** overdue.",
        f"Overall completion is **{summary['completion_rate']}%** with "
        f"**{summary['on_time_rate']}%** of finished work handed in on time.",
    ]
    return "  \n".join(lines)


def report_markdown(summary: dict, df: pd.DataFrame) -> str:
    name = profile["name"].strip() or "Student"
    lines = [
        f"# Studypilot progress report — {name}",
        "",
        f"Generated {datetime.now():%d %b %Y, %H:%M}",
        "",
        "## This week",
        f"- Completed: {summary['completed_this_week']} (last week: {summary['completed_last_week']})",
        f"- Due before Sunday: {summary['due_this_week']}",
        f"- Overdue: {summary['overdue']}",
        "",
        "## Overall",
        f"- Total assignments: {summary['total']}",
        f"- Completed: {summary['completed']} ({summary['completion_rate']}%)",
        f"- Pending: {summary['pending']}",
        f"- On-time completion rate: {summary['on_time_rate']}%",
        "",
        "## Assignments",
        "",
        "| Assignment | Course | Due | Priority | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    if not df.empty:
        for _, row in df.sort_values("due_at").iterrows():
            due_str = row["due_at"].strftime("%d %b %Y %H:%M") if hasattr(row["due_at"], "strftime") else str(row["due_at"])
            lines.append(
                f"| {row['title']} | {row['course'] or '—'} | {due_str} | "
                f"{row['priority']} | {row['status']} |"
            )
    return "\n".join(lines) + "\n"


df_all = db.fetch_assignments()
summary = db.summarise(df_all)

# --------------------------------------------------------------------------- #
# Sidebar: profile + Gmail settings
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### 🎓 Student profile")
    with st.form("profile_form"):
        new_name = st.text_input("Your name", value=profile["name"], placeholder="Ada Lovelace")
        new_email = st.text_input(
            "Notification email",
            value=profile["email"],
            placeholder="you@gmail.com",
            help="Reminders and progress reports are delivered here.",
        )
        auto_reminders = st.toggle(
            "Auto-send reminders when the app opens",
            value=bool(profile["auto_reminders"]),
        )
        if st.form_submit_button("Save profile", width="stretch"):
            db.save_profile(new_name, new_email, auto_reminders)
            profile = db.get_profile()
            flash("Profile saved.")
            st.rerun()

    st.divider()
    st.markdown("### 🔔 Gmail connection")
    st.text_input(
        "Gmail address",
        key="gmail_user",
        placeholder="you@gmail.com",
        help="The Google account that sends your reminders.",
    )
    st.text_input(
        "Gmail app password",
        key="gmail_pass",
        type="password",
        placeholder="16-character app password",
        help="Google Account → Security → 2-Step Verification → App passwords. "
        "A normal Gmail password will be rejected by Google.",
    )
    gmail_ready = bool(st.session_state["gmail_user"] and st.session_state["gmail_pass"])
    if gmail_ready and profile["email"]:
        st.success("Gmail is ready to send.", icon="✅")
    else:
        st.info("Add a Gmail address + app password to enable email reminders.", icon="✉️")
    st.caption("Your app password is kept in this session only — never written to the database.")

    st.divider()
    st.markdown("### 🧹 Data")
    st.caption(f"SQLite file: `{db.DB_PATH.relative_to(db.APP_DIR)}`")
    if st.button("Load sample assignments", width="stretch"):
        db.seed_demo_data()
        flash("Sample assignments added.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
student = profile["name"].strip() or "Student"
hello = "Good morning" if datetime.now().hour < 12 else (
    "Good afternoon" if datetime.now().hour < 18 else "Good evening"
)
st.markdown(
    f"""
    <div class="hero">
      <div>
        <h1>Studypilot</h1>
        <p>{hello}, {student} — {summary['pending']} assignment(s) open,
        {summary['overdue']} overdue.</p>
      </div>
      <div class="chip">🎯 Overall completion {summary['completion_rate']}%</div>
    </div>
    """,
    unsafe_allow_html=True,
)
show_flash()

# --------------------------------------------------------------------------- #
# Auto reminders (once per session, respects the 12h cooldown)
# --------------------------------------------------------------------------- #
if profile["auto_reminders"] and gmail_ready and profile["email"]:
    if not st.session_state.get("_auto_reminder_checked"):
        st.session_state["_auto_reminder_checked"] = True
        due = db.reminders_to_send(df_all)
        if not due.empty:
            ok, message = send_reminder(due.to_dict("records"))
            if ok:
                db.mark_reminded(due["id"].tolist())
                st.toast(f"Auto-reminder sent for {len(due)} assignment(s).", icon="📧")
            else:
                st.warning(message)

add_tab, list_tab, remind_tab, summary_tab = st.tabs(
    ["➕ Add assignment", "📋 To-do list", "🔔 Reminders", "📊 Progress & summary"]
)

# --------------------------------------------------------------------------- #
# Tab 1 — add assignment
# --------------------------------------------------------------------------- #
with add_tab:
    st.markdown("#### New assignment")
    st.markdown(
        "<p class='section-note'>The reminder priority decides how early Studypilot emails "
        "you — High 3 days before the deadline, Medium 2 days, Low 1 day.</p>",
        unsafe_allow_html=True,
    )
    with st.form("add_form", clear_on_submit=True):
        c1, c2 = st.columns([0.6, 0.4])
        title = c1.text_input("Assignment / task", placeholder="e.g. Linear algebra problem set 4")
        course = c2.text_input("Course", placeholder="e.g. MATH 201")

        c3, c4, c5 = st.columns([0.32, 0.3, 0.38])
        due_date = c3.date_input("Due date", value=date.today() + timedelta(days=3))
        due_time = c4.time_input("Due time", value=time(17, 0))
        priority = c5.selectbox(
            "Reminder priority",
            PRIORITY_OPTIONS,
            index=1,
            format_func=lambda p: (
                f"{db.PRIORITY_RULES[p]['emoji']} {p} — {db.PRIORITY_RULES[p]['hint']}"
            ),
            help="How important is this assignment? Higher priority = earlier reminder.",
        )
        notes = st.text_area("Notes (optional)", height=80, placeholder="Word count, chapters, submission link…")

        submitted = st.form_submit_button("Add assignment", type="primary", width="stretch")

    if submitted:
        if not title.strip():
            st.error("Give the assignment a title first.")
        else:
            due_at = datetime.combine(due_date, due_time)
            db.add_assignment(title, course, due_at, priority, notes)
            flash(f"“{title.strip()}” added with {priority.lower()} reminder priority.")
            st.rerun()

    st.divider()
    st.markdown("#### Due in the next 7 days")
    if not df_all.empty:
        upcoming = df_all[
            (df_all["is_done"] == 0)
            & (df_all["due_at"] <= pd.Timestamp.now() + pd.Timedelta(days=7))
        ].sort_values(["due_at"])
    else:
        upcoming = pd.DataFrame(columns=db.EMPTY_COLUMNS)

    if upcoming.empty:
        st.caption("Nothing due in the next week — nice place to be.")
    else:
        for _, row in upcoming.iterrows():
            due_text, due_class = human_when(row)
            st.markdown(
                f"{priority_pill(row['priority'])} **{row['title']}** "
                f"<span class='pill pill-course'>{row['course'] or 'General'}</span>"
                f"<span class='pill {due_class}'>{due_text}</span> "
                f"<span class='section-note'>· {row['due_at']:%a %d %b, %H:%M}</span>",
                unsafe_allow_html=True,
            )

# --------------------------------------------------------------------------- #
# Tab 2 — to-do list
# --------------------------------------------------------------------------- #
with list_tab:
    st.markdown("#### Your to-do list")
    if df_all.empty:
        st.info("No assignments yet. Add one in the **Add assignment** tab.", icon="📝")
    else:
        f1, f2, f3 = st.columns([0.4, 0.34, 0.26])
        status_filter = f1.multiselect(
            "Show", STATUS_OPTIONS, default=["Pending", "Overdue"], key="status_filter"
        )
        all_courses = sorted({c for c in df_all["course"].fillna("") if c})
        course_filter = f2.multiselect("Course", all_courses, default=[], key="course_filter")
        sort_by = f3.selectbox(
            "Sort by",
            ["Due date (soonest)", "Priority (highest)", "Recently added"],
            key="sort_by",
        )

        view = df_all[df_all["status"].isin(status_filter)]
        if course_filter:
            view = view[view["course"].isin(course_filter)]

        if sort_by == "Priority (highest)":
            rank = {p: i for i, p in enumerate(PRIORITY_OPTIONS)}
            view = view.assign(_rank=view["priority"].map(lambda p: rank.get(p, 99))).sort_values(["_rank", "due_at"])
        elif sort_by == "Recently added":
            view = view.sort_values("created_at", ascending=False)
        else:
            view = view.sort_values("due_at")

        done_count = int(view["is_done"].sum())
        st.progress(
            done_count / len(view) if len(view) else 0.0,
            text=f"{done_count} of {len(view)} shown assignment(s) completed",
        )

        if view.empty:
            st.info("Nothing matches these filters.", icon="🔍")

        for _, row in view.iterrows():
            with st.container(border=True):
                check_col, body_col, action_col = st.columns([0.07, 0.66, 0.27], gap="small")
                done_checked = check_col.checkbox(
                    "Done",
                    value=bool(row["is_done"]),
                    key=f"done_{row['id']}",
                    label_visibility="collapsed",
                    help="Tick when the assignment is submitted.",
                )
                if done_checked != bool(row["is_done"]):
                    db.set_done(int(row["id"]), done_checked)
                    flash(
                        f"“{row['title']}” marked {'complete 🎉' if done_checked else 'as pending'}."
                    )
                    st.rerun()

                due_text, due_class = human_when(row)
                title_class = "task-title done" if int(row["is_done"]) else "task-title"
                notes_html = (
                    f"<div class='task-notes'>{row['notes']}</div>" if row["notes"] else ""
                )
                body_col.markdown(
                    f"<div class='{title_class}'>{row['title']}</div>"
                    f"<div style='margin-top:6px'>"
                    f"{priority_pill(row['priority'])}"
                    f"<span class='pill pill-course'>{row['course'] or 'General'}</span>"
                    f"<span class='pill {due_class}'>{due_text}</span>"
                    f"<span class='pill pill-due'>📅 {row['due_at']:%a %d %b, %H:%M}</span>"
                    f"</div>{notes_html}",
                    unsafe_allow_html=True,
                )

                with action_col:
                    b1, b2 = st.columns([0.62, 0.38])
                    if int(row["is_done"]) == 0:
                        if b1.button(
                            "Remind",
                            key=f"remind_{row['id']}",
                            width="stretch",
                            help="Email me about this assignment now.",
                        ):
                            ok, message = send_reminder([row])
                            if ok:
                                db.mark_reminded([int(row["id"])])
                                flash(message)
                            else:
                                flash(message, "error")
                            st.rerun()
                    with b2.popover("⋯", width="stretch"):
                        created_str = row['created_at'].strftime('%d %b %Y') if hasattr(row['created_at'], 'strftime') and pd.notna(row['created_at']) else '—'
                        st.caption(f"Created {created_str}")
                        if pd.notna(row["last_reminder_at"]) and hasattr(row["last_reminder_at"], "strftime"):
                            st.caption(f"Last reminder {row['last_reminder_at']:%d %b, %H:%M}")
                        else:
                            st.caption("No reminder sent yet")
                        if st.button(
                            "Delete assignment",
                            key=f"delete_{row['id']}",
                            type="primary",
                            width="stretch",
                        ):
                            db.delete_assignment(int(row["id"]))
                            flash(f"“{row['title']}” deleted.")
                            st.rerun()

# --------------------------------------------------------------------------- #
# Tab 3 — reminders
# --------------------------------------------------------------------------- #
with remind_tab:
    st.markdown("#### Gmail reminders")
    rule_cols = st.columns(3)
    for col, priority in zip(rule_cols, PRIORITY_OPTIONS):
        rule = db.PRIORITY_RULES[priority]
        col.markdown(
            f"<div class='chip' style='background:#F8F9FD;color:#1F2430'>"
            f"{rule['emoji']} <b>{priority}</b> · reminds {rule['remind_days_before']} day"
            f"{'s' if rule['remind_days_before'] > 1 else ''} before</div>",
            unsafe_allow_html=True,
        )

    if not gmail_ready or not profile["email"]:
        st.warning(
            "Email is not fully configured yet. Add your Gmail address, an app password "
            "and a notification email in the sidebar.",
            icon="🔑",
        )

    due_now = db.reminders_to_send(df_all)
    st.divider()
    if due_now.empty:
        st.success("You are all caught up — no reminders are due right now.", icon="🎉")
    else:
        st.markdown(f"**{len(due_now)} reminder(s) ready to send**")
        for _, row in due_now.iterrows():
            due_text, due_class = human_when(row)
            with st.container(border=True):
                c1, c2 = st.columns([0.75, 0.25])
                c1.markdown(
                    f"**{row['title']}**  \n"
                    f"{priority_pill(row['priority'])}"
                    f"<span class='pill pill-course'>{row['course'] or 'General'}</span>"
                    f"<span class='pill {due_class}'>{due_text}</span>",
                    unsafe_allow_html=True,
                )
                if c2.button("Send now", key=f"send_{row['id']}", width="stretch"):
                    ok, message = send_reminder([row])
                    if ok:
                        db.mark_reminded([int(row["id"])])
                        flash(message)
                    else:
                        flash(message, "error")
                    st.rerun()

        b1, b2 = st.columns([0.5, 0.5])
        if b1.button(
            "Send all as one email",
            type="primary",
            width="stretch",
            key="send_digest",
        ):
            ok, message = send_reminder(due_now.to_dict("records"))
            if ok:
                db.mark_reminded(due_now["id"].tolist())
                flash(f"One email sent covering {len(due_now)} assignment(s).")
            else:
                flash(message, "error")
            st.rerun()

        with b2.popover("Preview email", width="stretch"):
            preview_rows = due_now.head(3).to_dict("records")
            subject, text, _ = mailer.build_reminder(
                profile["name"],
                [
                    {
                        "title": r["title"],
                        "course": r["course"],
                        "due_at": r["due_at"].to_pydatetime() if hasattr(r["due_at"], "to_pydatetime") else r["due_at"],
                        "priority": r["priority"],
                        "notes": r["notes"],
                        "days_left": float(r["days_left"]),
                    }
                    for r in preview_rows
                ],
            )
            st.caption(f"Subject: {subject}")
            st.code(text, language="text")

    st.divider()
    st.markdown("#### Coming up")
    waiting = df_all[(df_all["is_done"] == 0)]
    if waiting.empty:
        st.caption("No open assignments.")
    else:
        schedule = []
        for _, row in waiting.iterrows():
            days_before = db.PRIORITY_RULES.get(row["priority"], db.PRIORITY_RULES["Medium"])[
                "remind_days_before"
            ]
            remind_on = row["due_at"] - pd.Timedelta(days=days_before)
            remind_str = remind_on.strftime("%a %d %b, %H:%M") if hasattr(remind_on, "strftime") else str(remind_on)
            due_str = row["due_at"].strftime("%a %d %b, %H:%M") if hasattr(row["due_at"], "strftime") else str(row["due_at"])
            schedule.append(
                {
                    "Assignment": row["title"],
                    "Priority": row["priority"],
                    "Reminder goes out": remind_str,
                    "Due": due_str,
                }
            )
        st.dataframe(pd.DataFrame(schedule), hide_index=True)

    st.divider()
    st.markdown("#### Share my progress")
    st.caption("Email yourself this week's summary and the list of open assignments.")
    if st.button("📧 Email me my progress report"):
        open_rows = df_all[df_all["is_done"] == 0].sort_values("due_at")
        assignments = [
            {
                "title": r["title"],
                "course": r["course"],
                "due_at": r["due_at"].to_pydatetime() if hasattr(r["due_at"], "to_pydatetime") else r["due_at"],
                "priority": r["priority"],
                "notes": r["notes"],
                "days_left": float(r["days_left"]),
            }
            for r in open_rows.to_dict("records")
        ]
        subject, text, html = mailer.build_report(profile["name"], summary, assignments)
        ok, message = mailer.send_email(
            st.session_state["gmail_user"],
            st.session_state["gmail_pass"],
            profile["email"],
            subject,
            text,
            html,
        )
        if ok:
            flash("Progress report sent to your inbox.")
        else:
            flash(message, "error")

# --------------------------------------------------------------------------- #
# Tab 4 — progress & summary
# --------------------------------------------------------------------------- #
with summary_tab:
    st.markdown("#### Weekly & overall progress")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Overall completion", f"{summary['completion_rate']}%", f"{summary['completed']}/{summary['total']} done")
    m2.metric("Completed this week", summary["completed_this_week"],
              f"{summary['completed_this_week'] - summary['completed_last_week']:+d} vs last week")
    m3.metric("Due this week", summary["due_this_week"])
    m4.metric("Overdue", summary["overdue"],
              None if summary["overdue"] == 0 else "needs attention", delta_color="inverse")

    with st.container(border=True):
        st.markdown(weekly_narrative(summary))

    st.divider()
    chart_left, chart_mid, chart_right = st.columns([0.34, 0.36, 0.3])
    with chart_left:
        st.markdown("##### Completion ring")
        fig = charts.completion_donut(
            {"Completed": summary["completed"], "Pending": summary["pending"] - summary["overdue"],
             "Overdue": summary["overdue"]},
            center_label="of assignments done",
        )
        st.pyplot(fig)
        plt.close(fig)
    with chart_mid:
        st.markdown("##### Last 7 days")
        fig = charts.weekly_radial(summary["days"], summary["day_counts"])
        st.pyplot(fig, width="stretch")
        plt.close(fig)
    with chart_right:
        st.markdown("##### Open by priority")
        for priority in PRIORITY_OPTIONS:
            count = summary["by_priority"].get(priority, 0)
            rule = db.PRIORITY_RULES[priority]
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;padding:8px 10px;"
                f"border-radius:10px;background:#F8F9FD;margin-bottom:6px'>"
                f"<span>{rule['emoji']} {priority}</span><b>{count}</b></div>",
                unsafe_allow_html=True,
            )
        st.caption(f"On-time completion rate: **{summary['on_time_rate']}%**")

    st.divider()
    left, right = st.columns(2)
    with left:
        st.markdown("##### By course")
        if summary["by_course"].empty:
            st.caption("No assignments yet.")
        else:
            course_df = summary["by_course"].copy()
            total_series = course_df["total"].replace(0, 1)
            course_df["progress"] = (
                100 * course_df["completed"] / total_series
            ).round(0).astype(int).astype(str) + "%"
            course_df = course_df.rename(
                columns={"total": "Total", "completed": "Done", "progress": "Progress"}
            )
            st.dataframe(course_df[["Course", "Total", "Done", "Progress"]], hide_index=True)
    with right:
        st.markdown("##### Needs attention")
        if not df_all.empty:
            overdue_df = df_all[df_all["status"] == "Overdue"].sort_values("due_at")
        else:
            overdue_df = pd.DataFrame(columns=db.EMPTY_COLUMNS)
        if overdue_df.empty:
            st.success("Nothing overdue. Keep it going.", icon="🌱")
        else:
            for _, row in overdue_df.head(6).iterrows():
                due_display = row['due_at'].strftime('%d %b') if hasattr(row['due_at'], 'strftime') else str(row['due_at'])
                st.markdown(
                    f"- **{row['title']}** · {row['course'] or 'General'} · "
                    f"was due {due_display}"
                )

    st.divider()
    st.markdown("##### Download your progress")
    st.caption(
        "A spreadsheet with every assignment and a Markdown report you can paste into "
        "a journal, portfolio or send to a tutor."
    )
    export_cols = ["title", "course", "due_at", "priority", "status", "completed_at", "notes"]
    export_df = df_all[export_cols].copy()
    if not export_df.empty:
        export_df["due_at"] = pd.to_datetime(export_df["due_at"]).dt.strftime("%Y-%m-%d %H:%M")
        export_df["completed_at"] = pd.to_datetime(export_df["completed_at"]).dt.strftime("%Y-%m-%d %H:%M")
    export_df = export_df.rename(
        columns={
            "title": "Assignment", "course": "Course", "due_at": "Due",
            "priority": "Priority", "status": "Status",
            "completed_at": "Completed at", "notes": "Notes",
        }
    )
    stamp = date.today().isoformat()
    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ Download progress (CSV)",
        data=export_df.to_csv(index=False).encode("utf-8"),
        file_name=f"studypilot-progress-{stamp}.csv",
        mime="text/csv",
        width="stretch",
    )
    d2.download_button(
        "⬇️ Download weekly report (Markdown)",
        data=report_markdown(summary, df_all).encode("utf-8"),
        file_name=f"studypilot-report-{stamp}.md",
        mime="text/markdown",
        width="stretch",
    )

    with st.expander("How your data is stored"):
        st.markdown(
            f"""
- Everything lives in a local **SQLite** database at `{db.DB_PATH.name}` (folder `data/`).
- Each row in `assignments` stores the title, course, due date, **reminder priority**,
  notes, completion flag and the timestamp of the last reminder sent.
- The reminder schedule is derived: due date minus the priority window
  (High = 3 days, Medium = 2 days, Low = 1 day), with a 12-hour cooldown between emails.
            """
        )
