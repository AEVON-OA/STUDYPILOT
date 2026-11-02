# 🎓 Studypilot — Student Assignment Planner & To-Do App

Single-file Streamlit app to track assignments, get priority-based Gmail reminders, and see progress with circular charts. Data is stored locally in SQLite.

## Features

- **Add / manage assignments** — title, course, due date & time, priority, notes; mark done, delete.
- **Priority-based reminders** — the priority decides how early the email goes out:

  | Priority | Reminder sent |
  | --- | --- |
  | 🔴 High | 3 days before due |
  | 🟠 Medium | 2 days before due |
  | 🟢 Low | 1 day before due |

  A 12-hour cooldown prevents repeat emails for the same assignment.
- **Gmail integration** — manual "Send now", email preview, auto-reminders on app open (once per session), and an emailed progress report.
- **Progress dashboard** — completion donut, last-7-days radial chart, per-course and per-priority breakdown, on-time completion rate, overdue list.
- **Export** — CSV of all assignments and a Markdown weekly report.
- **Demo data** — "Load sample assignments" button in the sidebar.

## Project structure

```
studypilot/
├── app.py                         # Whole app (db, charts, mailer, Streamlit UI)
├── requirements.txt
├── README.md
├── .gitignore
├── .streamlit/
│   └── secrets.toml.example       # Template for Gmail credentials
└── data/
    └── studypilot.db              # Auto-created on first run (SQLite)
```

`app.py` is organised in sections: database & config → charts → email (SMTP) → Streamlit dashboard.

## Setup

Python 3.10+ recommended.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

App opens at http://localhost:8501.

## Gmail setup (for reminders)

Gmail rejects normal passwords over SMTP — you need an **App Password**.

1. Google Account → Security → turn on **2-Step Verification**.
2. Security → **App passwords** → create one → copy the 16-character code.
3. Provide it in any one of these ways:
   - **Sidebar** in the app (kept in session only, never saved to the DB), or
   - `.streamlit/secrets.toml` (copy from `secrets.toml.example`), or
   - Environment variables:
     ```bash
     export GMAIL_ADDRESS="you@gmail.com"
     export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
     ```
4. Set your name and **Notification email** in the sidebar profile and save.

Emails are sent via `smtp.gmail.com:465` (SSL).

## Data

- All data lives in `data/studypilot.db` (SQLite), created automatically.
- Tables: `assignments` (title, course, due_at, priority, notes, is_done, completed_at, created_at, last_reminder_at) and `profile` (name, email, auto_reminders).
- To reset everything, stop the app and delete `data/studypilot.db`.
- The `.db` file and `secrets.toml` are git-ignored.

## Notes

- Needs a recent Streamlit (`>=1.50`) because the code uses `width="stretch"` on widgets.
- Matplotlib runs with the non-GUI `Agg` backend, so it works on servers/headless setups.
- Never commit your app password.
