import io
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for

DATABASE = Path(__file__).parent / "timekeeper.db"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        migrate_db(g.db)
    return g.db


def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    cols = _table_columns(conn, "work_sessions")
    if "work_day_id" not in cols:
        conn.execute(
            "ALTER TABLE work_sessions ADD COLUMN work_day_id INTEGER REFERENCES work_days(id)"
        )
    conn.commit()
    if "work_day_id" in _table_columns(conn, "work_sessions"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_sessions_day ON work_sessions(work_day_id)"
        )
        conn.commit()
    _migrate_tasks_per_work_day(conn)
    _migrate_work_day_report_fields(conn)
    _migrate_work_day_calendar_days(conn)
    _migrate_drop_work_days_created_at(conn)
    _migrate_active_tasks(conn)
    _migrate_todo(conn)
    orphan = conn.execute(
        "SELECT COUNT(*) FROM work_sessions WHERE work_day_id IS NULL"
    ).fetchone()[0]
    if orphan > 0:
        d = datetime.now().astimezone().date().isoformat()
        cur = conn.execute(
            """
            INSERT INTO work_days (title, note, next_steps, questions, day_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Импорт (до обновления)", "", "", "", d),
        )
        wid = cur.lastrowid
        conn.execute(
            "UPDATE work_sessions SET work_day_id = ? WHERE work_day_id IS NULL",
            (wid,),
        )
        conn.commit()


def _migrate_active_tasks(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


DEFAULT_TODO_COLUMNS = [
    ("НА СТОРОНЕ - ОЖИДАЮ", "waiting"),
    ("ПРИНЯТЬ РЕШЕНИЕ", "decide"),
    ("ЗАПЛАНИРОВАНО", "planned"),
    ("В РАБОТЕ", "in_progress"),
    ("ГОТОВО", "done"),
    ("ПРЕДСТАВЛЕНО, ОДОБРЕНО", "approved"),
]

def _migrate_todo(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS todo_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todo_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES todo_projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            color_key TEXT NOT NULL DEFAULT 'planned',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS todo_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id INTEGER NOT NULL REFERENCES todo_columns(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_todo_columns_project ON todo_columns(project_id);
        CREATE INDEX IF NOT EXISTS idx_todo_cards_column ON todo_cards(column_id);
        """
    )
    conn.commit()


def create_default_todo_columns(conn, project_id: int):
    for order, (title, color_key) in enumerate(DEFAULT_TODO_COLUMNS):
        conn.execute(
            """
            INSERT INTO todo_columns (project_id, title, color_key, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, title, color_key, order),
        )


def list_todo_projects(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.title, p.note, p.created_at,
               (SELECT COUNT(*) FROM todo_columns c WHERE c.project_id = p.id) AS column_count,
               (
                   SELECT COUNT(*) FROM todo_cards card
                   JOIN todo_columns c ON c.id = card.column_id
                   WHERE c.project_id = p.id
               ) AS card_count
        FROM todo_projects p
        ORDER BY p.sort_order ASC, p.id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_todo_board(conn, project_id: int) -> dict | None:
    project = conn.execute(
        "SELECT id, title, note, created_at FROM todo_projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if not project:
        return None
    columns = []
    for col in conn.execute(
        """
        SELECT id, title, color_key, sort_order
        FROM todo_columns
        WHERE project_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (project_id,),
    ):
        cards = [
            dict(c)
            for c in conn.execute(
                """
                SELECT id, title, note, sort_order, created_at
                FROM todo_cards
                WHERE column_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (col["id"],),
            )
        ]
        columns.append(
            {
                "id": col["id"],
                "title": col["title"],
                "color_key": col["color_key"],
                "sort_order": col["sort_order"],
                "cards": cards,
            }
        )
    return {"project": dict(project), "columns": columns}


def _migrate_work_day_report_fields(conn):
    cols = _table_columns(conn, "work_days")
    if "next_steps" not in cols:
        conn.execute(
            "ALTER TABLE work_days ADD COLUMN next_steps TEXT NOT NULL DEFAULT ''"
        )
    if "questions" not in cols:
        conn.execute(
            "ALTER TABLE work_days ADD COLUMN questions TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


RU_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def parse_day_date_value(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v.strip())
    except (TypeError, ValueError):
        return None


def local_date_from_iso(iso_s: str) -> date:
    return parse_iso(iso_s).astimezone().date()


def format_work_day_title(d: date) -> str:
    return f"{d.strftime('%d.%m.%Y')}, {RU_WEEKDAYS[d.weekday()]}"


def _daterange_inclusive(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    out: list[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _parse_dates_from_title(title: str) -> list[date] | None:
    raw = (title or "").strip()
    if not raw:
        return None
    low = raw.lower()
    m = re.search(
        r"недел\w*\s+(\d{1,2})\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})",
        low,
        flags=re.UNICODE,
    )
    if m:
        d1, d2, mo, y = (int(m.group(i)) for i in range(1, 5))
        return _daterange_inclusive(date(y, mo, d1), date(y, mo, d2))
    m = re.match(
        r"^(\d{2})\.(\d{2})\.(\d{4})\s*[–—\-]\s*(\d{2})\.(\d{2})\.(\d{4})",
        raw,
    )
    if m:
        start = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        end = date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
        return _daterange_inclusive(start, end)
    m = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})\.(\d{2})\.(\d{4})$", raw)
    if m:
        d1, d2, mo, y = (int(m.group(i)) for i in range(1, 5))
        return _daterange_inclusive(date(y, mo, d1), date(y, mo, d2))
    return None


def _primary_date_from_title(title: str) -> date:
    raw = (title or "").strip()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return datetime.now().astimezone().date()


def _task_id_for_day_name(conn, work_day_id: int, name: str) -> int:
    existing = conn.execute(
        "SELECT id FROM tasks WHERE work_day_id = ? AND name = ? COLLATE NOCASE",
        (work_day_id, name),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO tasks (work_day_id, name, created_at) VALUES (?, ?, ?)",
        (work_day_id, name, iso(utc_now())),
    )
    return cur.lastrowid


def _move_session_to_day(conn, session_id: int, old_task_id: int, target_day_id: int):
    name_row = conn.execute("SELECT name FROM tasks WHERE id = ?", (old_task_id,)).fetchone()
    if not name_row:
        conn.execute("DELETE FROM work_sessions WHERE id = ?", (session_id,))
        return
    new_task_id = _task_id_for_day_name(conn, target_day_id, name_row["name"])
    conn.execute(
        "UPDATE work_sessions SET work_day_id = ?, task_id = ? WHERE id = ?",
        (target_day_id, new_task_id, session_id),
    )


def _append_note_block(base: str, extra: str) -> str:
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return base
    if not base:
        return extra
    return base + "\n\n" + extra


def _merge_work_days(conn, keep_id: int, drop_id: int):
    if keep_id == drop_id:
        return
    drop = conn.execute(
        "SELECT note, next_steps, questions, title FROM work_days WHERE id = ?",
        (drop_id,),
    ).fetchone()
    keep = conn.execute(
        "SELECT note, next_steps, questions FROM work_days WHERE id = ?",
        (keep_id,),
    ).fetchone()
    if drop and keep:
        conn.execute(
            """
            UPDATE work_days
            SET note = ?, next_steps = ?, questions = ?
            WHERE id = ?
            """,
            (
                _append_note_block(keep["note"], drop["note"]),
                _append_note_block(keep["next_steps"], drop["next_steps"]),
                _append_note_block(keep["questions"], drop["questions"]),
                keep_id,
            ),
        )
    for s in conn.execute(
        "SELECT id, task_id FROM work_sessions WHERE work_day_id = ?",
        (drop_id,),
    ):
        _move_session_to_day(conn, s["id"], s["task_id"], keep_id)
    conn.execute("DELETE FROM tasks WHERE work_day_id = ?", (drop_id,))
    conn.execute("DELETE FROM work_days WHERE id = ?", (drop_id,))


def get_work_day_id_for_date(conn, d: date) -> int | None:
    row = conn.execute(
        "SELECT id FROM work_days WHERE day_date = ?",
        (d.isoformat(),),
    ).fetchone()
    return row["id"] if row else None


def get_or_create_work_day_for_date(
    conn,
    d: date,
    *,
    note: str = "",
    next_steps: str = "",
    questions: str = "",
) -> int:
    existing_id = get_work_day_id_for_date(conn, d)
    if existing_id is not None:
        return existing_id
    title = format_work_day_title(d)
    cur = conn.execute(
        """
        INSERT INTO work_days (title, note, next_steps, questions, day_date)
        VALUES (?, ?, ?, ?, ?)
        """,
        (title, note, next_steps, questions, d.isoformat()),
    )
    return cur.lastrowid


def _cleanup_orphan_tasks(conn, work_day_id: int):
    conn.execute(
        """
        DELETE FROM tasks
        WHERE work_day_id = ?
          AND id NOT IN (SELECT task_id FROM work_sessions WHERE work_day_id = ?)
        """,
        (work_day_id, work_day_id),
    )


def _migrate_work_day_calendar_days(conn):
    cols = _table_columns(conn, "work_days")
    if "day_date" not in cols:
        conn.execute("ALTER TABLE work_days ADD COLUMN day_date TEXT")
        conn.commit()

    pending = conn.execute(
        "SELECT COUNT(*) FROM work_days WHERE day_date IS NULL OR day_date = ''"
    ).fetchone()[0]
    multi = 0
    for row in conn.execute(
        "SELECT id, title FROM work_days WHERE day_date IS NULL OR day_date = ''"
    ):
        if _parse_dates_from_title(row["title"] or ""):
            multi += 1
    if pending == 0 and multi == 0:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_days_day_date ON work_days(day_date)"
        )
        conn.commit()
        return

    for wd in conn.execute(
        """
        SELECT id, title, note, next_steps, questions
        FROM work_days
        ORDER BY id ASC
        """
    ).fetchall():
        dates = _parse_dates_from_title(wd["title"] or "")
        if not dates or len(dates) <= 1:
            continue
        old_id = wd["id"]
        id_by_date: dict[date, int] = {}
        for i, d in enumerate(dates):
            wid = get_or_create_work_day_for_date(
                conn,
                d,
                note=wd["note"] if i == 0 else "",
                next_steps=wd["next_steps"] if i == 0 else "",
                questions=wd["questions"] if i == 0 else "",
            )
            id_by_date[d] = wid
            conn.execute(
                "UPDATE work_days SET day_date = ?, title = ? WHERE id = ?",
                (d.isoformat(), format_work_day_title(d), wid),
            )
        for s in conn.execute(
            "SELECT id, task_id, started_at FROM work_sessions WHERE work_day_id = ?",
            (old_id,),
        ):
            sd = local_date_from_iso(s["started_at"])
            target_id = id_by_date.get(sd)
            if target_id is None:
                target_id = get_or_create_work_day_for_date(conn, sd)
                conn.execute(
                    "UPDATE work_days SET day_date = ?, title = ? WHERE id = ?",
                    (sd.isoformat(), format_work_day_title(sd), target_id),
                )
            _move_session_to_day(conn, s["id"], s["task_id"], target_id)
        _cleanup_orphan_tasks(conn, old_id)
        if old_id not in id_by_date.values():
            conn.execute("DELETE FROM work_sessions WHERE work_day_id = ?", (old_id,))
            conn.execute("DELETE FROM tasks WHERE work_day_id = ?", (old_id,))
            conn.execute("DELETE FROM work_days WHERE id = ?", (old_id,))

    for wd in conn.execute(
        """
        SELECT id, title, note, next_steps, questions
        FROM work_days
        WHERE day_date IS NULL OR day_date = ''
        ORDER BY id ASC
        """
    ).fetchall():
        d = _primary_date_from_title(wd["title"] or "")
        existing = get_work_day_id_for_date(conn, d)
        if existing is not None and existing != wd["id"]:
            _merge_work_days(conn, existing, wd["id"])
            continue
        conn.execute(
            """
            UPDATE work_days
            SET day_date = ?, title = ?
            WHERE id = ?
            """,
            (d.isoformat(), format_work_day_title(d), wd["id"]),
        )

    for wd in conn.execute(
        "SELECT id, day_date FROM work_days WHERE day_date IS NOT NULL AND day_date != ''"
    ).fetchall():
        day_d = parse_day_date_value(wd["day_date"])
        if not day_d:
            continue
        for s in conn.execute(
            "SELECT id, task_id, started_at FROM work_sessions WHERE work_day_id = ?",
            (wd["id"],),
        ):
            sd = local_date_from_iso(s["started_at"])
            if sd == day_d:
                continue
            target_id = get_or_create_work_day_for_date(conn, sd)
            conn.execute(
                "UPDATE work_days SET day_date = ?, title = ? WHERE id = ?",
                (sd.isoformat(), format_work_day_title(sd), target_id),
            )
            _move_session_to_day(conn, s["id"], s["task_id"], target_id)
        _cleanup_orphan_tasks(conn, wd["id"])

    for row in conn.execute(
        """
        SELECT day_date, GROUP_CONCAT(id) AS ids
        FROM work_days
        WHERE day_date IS NOT NULL AND day_date != ''
        GROUP BY day_date
        HAVING COUNT(*) > 1
        """
    ).fetchall():
        ids = sorted(int(x) for x in row["ids"].split(","))
        keep_id = ids[0]
        for drop_id in ids[1:]:
            _merge_work_days(conn, keep_id, drop_id)

    for wd in conn.execute(
        "SELECT id, day_date FROM work_days WHERE day_date IS NOT NULL AND day_date != ''"
    ):
        d = parse_day_date_value(wd["day_date"])
        if d:
            conn.execute(
                "UPDATE work_days SET title = ? WHERE id = ?",
                (format_work_day_title(d), wd["id"]),
            )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_days_day_date ON work_days(day_date)"
    )
    conn.commit()


def _migrate_drop_work_days_created_at(conn):
    cols = _table_columns(conn, "work_days")
    if "created_at" not in cols:
        return
    for wd in conn.execute(
        "SELECT id, title, day_date FROM work_days WHERE day_date IS NULL OR day_date = ''"
    ):
        d = parse_day_date_value(wd["day_date"]) or _primary_date_from_title(
            wd["title"] or ""
        )
        conn.execute(
            "UPDATE work_days SET day_date = ?, title = ? WHERE id = ?",
            (d.isoformat(), format_work_day_title(d), wd["id"]),
        )
    conn.executescript(
        """
        CREATE TABLE work_days_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            next_steps TEXT NOT NULL DEFAULT '',
            questions TEXT NOT NULL DEFAULT '',
            day_date TEXT
        );
        INSERT INTO work_days_new (id, title, note, next_steps, questions, day_date)
        SELECT id, title, note, next_steps, questions, day_date FROM work_days;
        DROP TABLE work_days;
        ALTER TABLE work_days_new RENAME TO work_days;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_work_days_day_date ON work_days(day_date);
        """
    )
    conn.commit()


def _migrate_tasks_per_work_day(conn):
    tcols = _table_columns(conn, "tasks")
    if "work_day_id" not in tcols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN work_day_id INTEGER REFERENCES work_days(id)"
        )
        conn.commit()
    for row in conn.execute("SELECT id FROM tasks WHERE work_day_id IS NULL"):
        tid = row["id"]
        s = conn.execute(
            "SELECT work_day_id FROM work_sessions WHERE task_id = ? LIMIT 1",
            (tid,),
        ).fetchone()
        if s:
            conn.execute(
                "UPDATE tasks SET work_day_id = ? WHERE id = ?",
                (s["work_day_id"], tid),
            )
        else:
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    conn.commit()
    for ws in conn.execute(
        """
        SELECT ws.id AS sid, ws.task_id, ws.work_day_id AS wid
        FROM work_sessions ws
        JOIN tasks t ON t.id = ws.task_id
        WHERE t.work_day_id != ws.work_day_id
        """
    ).fetchall():
        name = conn.execute("SELECT name FROM tasks WHERE id = ?", (ws["task_id"],)).fetchone()[
            "name"
        ]
        wid = ws["wid"]
        existing = conn.execute(
            "SELECT id FROM tasks WHERE work_day_id = ? AND name = ? COLLATE NOCASE",
            (wid, name),
        ).fetchone()
        if existing:
            nid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO tasks (work_day_id, name, created_at) VALUES (?, ?, ?)",
                (wid, name, iso(utc_now())),
            )
            nid = cur.lastrowid
        conn.execute(
            "UPDATE work_sessions SET task_id = ? WHERE id = ?",
            (nid, ws["sid"]),
        )
    conn.commit()
    if "work_day_id" in _table_columns(conn, "tasks"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_work_day ON tasks(work_day_id)"
        )
        conn.commit()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS work_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            next_steps TEXT NOT NULL DEFAULT '',
            questions TEXT NOT NULL DEFAULT '',
            day_date TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_day_id INTEGER NOT NULL REFERENCES work_days(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            work_day_id INTEGER NOT NULL REFERENCES work_days(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_work_sessions_task ON work_sessions(task_id);
        CREATE TABLE IF NOT EXISTS active_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todo_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todo_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES todo_projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            color_key TEXT NOT NULL DEFAULT 'planned',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS todo_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id INTEGER NOT NULL REFERENCES todo_columns(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    migrate_db(db)
    db.close()


app = Flask(__name__)
app.teardown_appcontext(close_db)


def redirect_back(default_endpoint: str = "work_days_home", **kwargs):
    referrer = request.referrer
    if referrer and referrer.startswith(request.host_url):
        return redirect(referrer)
    return redirect(url_for(default_endpoint, **kwargs))


def list_active_tasks(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, sort_order, created_at
        FROM active_tasks
        ORDER BY sort_order ASC, name COLLATE NOCASE ASC, id ASC
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "sort_order": r["sort_order"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.context_processor
def inject_active_tasks_panel():
    conn = get_db()
    panel_work_day_id = None
    if request.endpoint == "work_day_view" and request.view_args:
        panel_work_day_id = request.view_args.get("work_day_id")
    active = active_session(conn)
    return {
        "active_tasks": list_active_tasks(conn),
        "panel_work_day_id": panel_work_day_id,
        "nav_active_work_day_id": active["work_day_id"] if active else None,
    }


@app.before_request
def ensure_db():
    if not DATABASE.exists():
        init_db()


def stop_all_open_sessions(conn):
    now = iso(utc_now())
    conn.execute(
        "UPDATE work_sessions SET ended_at = ? WHERE ended_at IS NULL",
        (now,),
    )


def active_session(conn):
    row = conn.execute(
        """
        SELECT ws.id, ws.task_id, ws.started_at, ws.work_day_id,
               t.name AS task_name, wd.title AS work_day_title
        FROM work_sessions ws
        JOIN tasks t ON t.id = ws.task_id
        JOIN work_days wd ON wd.id = ws.work_day_id
        WHERE ws.ended_at IS NULL
        ORDER BY ws.id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def session_duration_seconds(started_at: str, ended_at: str | None) -> float:
    start = parse_iso(started_at)
    end = parse_iso(ended_at) if ended_at else utc_now()
    return max(0.0, (end - start).total_seconds())


def work_day_total_seconds(conn, work_day_id: int) -> float:
    total = 0.0
    for r in conn.execute(
        "SELECT started_at, ended_at FROM work_sessions WHERE work_day_id = ?",
        (work_day_id,),
    ):
        total += session_duration_seconds(r["started_at"], r["ended_at"])
    return total


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_worked(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m or h:
        parts.append(f"{m} мин")
    parts.append(f"{sec} с")
    return "отработано " + " ".join(parts)


def fmt_worked_value(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m or h:
        parts.append(f"{m} мин")
    parts.append(f"{sec} с")
    return " ".join(parts)


def fmt_created(iso_s: str) -> str:
    try:
        return parse_iso(iso_s).astimezone().strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return iso_s or ""


def fmt_day_date(day_date: str | None = None) -> str:
    parsed = parse_day_date_value(day_date)
    if parsed:
        return parsed.strftime("%d.%m.%Y")
    return ""


def format_bullet_lines(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("•"):
            line = "• " + line.lstrip("-•* ").strip()
        lines.append(line)
    return lines


def format_text_block(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def format_single_day_report_text(work_day: dict, tasks: list, summary: dict) -> str:
    lines = [
        f"Рабочий день: {fmt_day_date(work_day.get('day_date')) or work_day.get('title') or '—'}",
        "",
        f"Всего времени: {summary['total_duration']}",
        f"Задачи > {summary['task_count']} <:",
    ]
    for idx, t in enumerate(tasks, start=1):
        lines.append(f"{idx}. {t['name']} — {t['duration']} ({t['session_count']} сессий)")
    lines.append("")
    lines.append("Итоги работы:")
    bullets = format_bullet_lines(work_day.get("note") or "")
    if bullets:
        lines.extend(bullets)
    lines.append("")
    lines.append("Дальнейшее направление работы (активные задачи):")
    next_steps = format_text_block(work_day.get("next_steps") or "")
    if next_steps:
        lines.extend(next_steps)
    else:
        lines.append("-")
    lines.append("")
    lines.append("Вопросы|проблемы:")
    questions = format_text_block(work_day.get("questions") or "")
    if questions:
        lines.extend(questions)
    else:
        lines.append("-")
    return "\n".join(lines)


def build_work_days_overview(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, title, note, next_steps, questions, day_date
        FROM work_days
        ORDER BY id DESC
        """
    ).fetchall()
    days = []
    for r in rows:
        sec = work_day_total_seconds(conn, r["id"])
        n_sess = conn.execute(
            "SELECT COUNT(*) FROM work_sessions WHERE work_day_id = ?",
            (r["id"],),
        ).fetchone()[0]
        n_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE work_day_id = ?",
            (r["id"],),
        ).fetchone()[0]
        days.append(
            {
                "id": r["id"],
                "title": r["title"],
                "day_date": r["day_date"] or "",
                "note": r["note"] or "",
                "next_steps": r["next_steps"] or "",
                "questions": r["questions"] or "",
                "seconds": sec,
                "session_count": n_sess,
                "task_count": n_tasks,
            }
        )
    return days


def _parse_float(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return None


def _parse_input_date(v: str | None):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v).date()
    except (TypeError, ValueError):
        return None


def current_work_week_bounds(reference_date=None):
    today = reference_date or datetime.now().date()
    # Monday=0 ... Sunday=6; our week starts on Wednesday (2)
    days_since_wed = (today.weekday() - 2) % 7
    start = today.fromordinal(today.toordinal() - days_since_wed)
    end = today.fromordinal(start.toordinal() + 6)
    return start, end


def parse_work_day_filters(args) -> dict:
    sort_by = (args.get("sort_by") or "day_date").strip()
    sort_dir = (args.get("sort_dir") or "desc").strip().lower()
    has_note = (args.get("has_note") or "any").strip().lower()
    if sort_by not in {"day_date", "created_at", "title", "seconds", "task_count", "session_count"}:
        sort_by = "day_date"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"
    if has_note not in {"any", "yes", "no"}:
        has_note = "any"
    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    if not date_from and not date_to:
        week_start, week_end = current_work_week_bounds()
        date_from = week_start.isoformat()
        date_to = week_end.isoformat()
    return {
        "q": (args.get("q") or "").strip(),
        "date_from": date_from,
        "date_to": date_to,
        "min_hours": (args.get("min_hours") or "").strip(),
        "max_hours": (args.get("max_hours") or "").strip(),
        "has_note": has_note,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }


def work_day_sort_date(d: dict) -> date:
    parsed = parse_day_date_value(d.get("day_date"))
    if parsed:
        return parsed
    return _primary_date_from_title(d.get("title") or "")


def filter_and_sort_days(days: list[dict], filters: dict) -> list[dict]:
    q = (filters.get("q") or "").lower()
    date_from = _parse_input_date(filters.get("date_from"))
    date_to = _parse_input_date(filters.get("date_to"))
    min_hours = _parse_float(filters.get("min_hours"))
    max_hours = _parse_float(filters.get("max_hours"))
    has_note = filters.get("has_note") or "any"
    sort_by = filters.get("sort_by") or "day_date"
    if sort_by == "created_at":
        sort_by = "day_date"
    sort_dir = filters.get("sort_dir") or "desc"

    filtered = []
    for d in days:
        created_local = work_day_sort_date(d)
        hours = d["seconds"] / 3600.0
        note_text = (d["note"] or "").strip()
        if q:
            in_any = (
                q in d["title"].lower()
                or q in d["note"].lower()
                or q in fmt_day_date(d.get("day_date")).lower()
                or q in (d.get("day_date") or "").lower()
            )
            if not in_any:
                continue
        if date_from and created_local < date_from:
            continue
        if date_to and created_local > date_to:
            continue
        if min_hours is not None and hours < min_hours:
            continue
        if max_hours is not None and hours > max_hours:
            continue
        if has_note == "yes" and not note_text:
            continue
        if has_note == "no" and note_text:
            continue
        filtered.append(d)

    sorters = {
        "day_date": lambda d: work_day_sort_date(d).isoformat(),
        "created_at": lambda d: work_day_sort_date(d).isoformat(),
        "title": lambda d: d["title"].lower(),
        "seconds": lambda d: d["seconds"],
        "task_count": lambda d: d["task_count"],
        "session_count": lambda d: d["session_count"],
    }
    filtered.sort(key=sorters[sort_by], reverse=(sort_dir == "desc"))
    return filtered


def collect_multi_work_days_report(conn, days: list[dict], filters: dict) -> dict:
    export_days = []
    for d in days:
        day_report = collect_work_day_report(conn, d["id"])
        tasks = []
        if day_report:
            tasks = [
                {
                    "name": t["name"],
                    "duration": t["duration"],
                    "seconds": t["seconds"],
                    "session_count": t["session_count"],
                }
                for t in day_report.get("tasks", [])
            ]
        resolved_tasks_count = sum(1 for t in tasks if t["seconds"] > 0)
        item = {
            "id": d["id"],
            "title": d["title"],
            "note": d["note"],
            "next_steps": d.get("next_steps") or (day_report or {}).get("work_day", {}).get("next_steps", ""),
            "questions": d.get("questions") or (day_report or {}).get("work_day", {}).get("questions", ""),
            "day_date": d.get("day_date") or "",
            "day_local": fmt_day_date(d.get("day_date")),
            "seconds": d["seconds"],
            "formatted_duration": fmt_duration(d["seconds"]),
            "task_count": d["task_count"],
            "session_count": d["session_count"],
            "resolved_tasks_count": resolved_tasks_count,
            "tasks": tasks,
        }
        export_days.append(item)
    total_seconds = sum(d["seconds"] for d in export_days)
    total_tasks = sum(d["task_count"] for d in export_days)
    total_sessions = sum(d["session_count"] for d in export_days)
    return {
        "version": 1,
        "exported_at": iso(utc_now()),
        "filters": filters,
        "summary": {
            "days_count": len(export_days),
            "total_seconds": round(total_seconds, 2),
            "total_duration": fmt_duration(total_seconds),
            "task_count": total_tasks,
            "session_count": total_sessions,
        },
        "days": export_days,
    }


def multi_report_txt_text(report: dict) -> str:
    parts = []
    for d in report["days"]:
        day_report = {
            "work_day": {
                "day_date": d.get("day_date") or "",
                "note": d.get("note") or "",
                "next_steps": d.get("next_steps") or "",
                "questions": d.get("questions") or "",
            },
            "summary": {
                "total_duration": d["formatted_duration"],
                "task_count": d["task_count"],
            },
            "tasks": d.get("tasks") or [],
        }
        parts.append(
            format_single_day_report_text(
                day_report["work_day"],
                day_report["tasks"],
                day_report["summary"],
            )
        )
    return "\n\n".join(parts)


def collect_work_day_report(conn, work_day_id: int) -> dict | None:
    wd = conn.execute(
        """
        SELECT id, title, note, next_steps, questions, day_date
        FROM work_days WHERE id = ?
        """,
        (work_day_id,),
    ).fetchone()
    if not wd:
        return None
    tasks = []
    total_seconds = 0.0
    total_sessions = 0
    task_rows = conn.execute(
        """
        SELECT id, name FROM tasks
        WHERE work_day_id = ?
        ORDER BY name COLLATE NOCASE
        """,
        (work_day_id,),
    ).fetchall()
    for t in task_rows:
        sessions = []
        task_seconds = 0.0
        for s in conn.execute(
            """
            SELECT started_at, ended_at FROM work_sessions
            WHERE work_day_id = ? AND task_id = ?
            ORDER BY started_at
            """,
            (work_day_id, t["id"]),
        ).fetchall():
            sec = session_duration_seconds(s["started_at"], s["ended_at"])
            task_seconds += sec
            sessions.append(
                {
                    "started_at": s["started_at"],
                    "ended_at": s["ended_at"],
                    "seconds": round(sec, 2),
                    "duration": fmt_duration(sec),
                }
            )
        total_seconds += task_seconds
        total_sessions += len(sessions)
        tasks.append(
            {
                "name": t["name"],
                "seconds": round(task_seconds, 2),
                "duration": fmt_duration(task_seconds),
                "session_count": len(sessions),
                "sessions": sessions,
            }
        )
    tasks.sort(key=lambda x: (-x["seconds"], x["name"].lower()))
    return {
        "version": 1,
        "exported_at": iso(utc_now()),
        "work_day": {
            "id": wd["id"],
            "title": wd["title"],
            "day_date": wd["day_date"] or "",
            "note": wd["note"] or "",
            "next_steps": wd["next_steps"] or "",
            "questions": wd["questions"] or "",
        },
        "summary": {
            "total_seconds": round(total_seconds, 2),
            "total_duration": fmt_duration(total_seconds),
            "task_count": len(tasks),
            "session_count": total_sessions,
        },
        "tasks": tasks,
    }


def report_txt_text(report: dict) -> str:
    return format_single_day_report_text(
        report["work_day"],
        report["tasks"],
        report["summary"],
    )


def _safe_report_filename(title: str, ext: str) -> str:
    # HTTP header values in some local servers are latin-1 only, so keep filename ASCII-safe.
    base = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in ("-", "_"))) else "_"
        for ch in title.lower()
    ).strip("_")
    if not base:
        base = "report"
    return f"{base}.{ext}"


@app.route("/")
def work_days_home():
    conn = get_db()
    filters = parse_work_day_filters(request.args)
    days = filter_and_sort_days(build_work_days_overview(conn), filters)
    filtered_total_seconds = sum(d["seconds"] for d in days)
    active = active_session(conn)
    today = datetime.now().astimezone().date().isoformat()
    return render_template(
        "work_days.html",
        days=days,
        active=active,
        filters=filters,
        filtered_total_seconds=filtered_total_seconds,
        default_day_date=today,
        fmt=fmt_duration,
        fmt_worked=fmt_worked,
        fmt_worked_value=fmt_worked_value,
    )


@app.post("/work-days")
def create_work_day():
    note = (request.form.get("note") or "").strip()
    conn = get_db()
    d = parse_day_date_value(request.form.get("day_date"))
    if not d:
        d = datetime.now().astimezone().date()
    existing = get_work_day_id_for_date(conn, d)
    if existing is not None:
        conn.commit()
        return redirect(url_for("work_day_view", work_day_id=existing))
    wid = get_or_create_work_day_for_date(conn, d, note=note)
    conn.commit()
    return redirect(url_for("work_day_view", work_day_id=wid))


@app.get("/work-days/<int:work_day_id>")
def work_day_view(work_day_id: int):
    conn = get_db()
    wd = conn.execute(
        """
        SELECT id, title, note, next_steps, questions, day_date
        FROM work_days WHERE id = ?
        """,
        (work_day_id,),
    ).fetchone()
    if not wd:
        abort(404)
    active = active_session(conn)
    tasks = []
    for t in conn.execute(
        """
        SELECT id, name FROM tasks
        WHERE work_day_id = ?
        ORDER BY name COLLATE NOCASE
        """,
        (work_day_id,),
    ):
        sec = 0.0
        for r in conn.execute(
            """
            SELECT started_at, ended_at FROM work_sessions
            WHERE task_id = ? AND work_day_id = ?
            """,
            (t["id"], work_day_id),
        ):
            sec += session_duration_seconds(r["started_at"], r["ended_at"])
        tasks.append({"id": t["id"], "name": t["name"], "seconds": sec})
    tasks.sort(key=lambda x: (-x["seconds"], x["name"].lower()))
    day_total = work_day_total_seconds(conn, work_day_id)
    timer_on_this_day = active and active.get("work_day_id") == work_day_id
    return render_template(
        "work_day.html",
        wd=dict(wd),
        tasks=tasks,
        active=active,
        day_total_seconds=day_total,
        timer_on_this_day=timer_on_this_day,
        fmt=fmt_duration,
        task_names_api_url=url_for("api_task_names"),
    )


@app.get("/work-days/<int:work_day_id>/report-data")
def report_data(work_day_id: int):
    conn = get_db()
    report = collect_work_day_report(conn, work_day_id)
    if not report:
        abort(404)
    return jsonify(report)


@app.get("/work-days/<int:work_day_id>/report.txt")
def export_report_txt(work_day_id: int):
    conn = get_db()
    report = collect_work_day_report(conn, work_day_id)
    if not report:
        abort(404)
    filename = _safe_report_filename(report["work_day"]["title"], "txt")
    body = report_txt_text(report)
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/reports/multi-data")
def multi_report_data():
    conn = get_db()
    filters = parse_work_day_filters(request.args)
    days = filter_and_sort_days(build_work_days_overview(conn), filters)
    return jsonify(collect_multi_work_days_report(conn, days, filters))


@app.get("/reports/multi.txt")
def export_multi_report_txt():
    conn = get_db()
    filters = parse_work_day_filters(request.args)
    days = filter_and_sort_days(build_work_days_overview(conn), filters)
    report = collect_multi_work_days_report(conn, days, filters)
    body = multi_report_txt_text(report)
    filename = _safe_report_filename(f"work_days_{len(days)}", "txt")
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/work-days/<int:work_day_id>/update")
def work_day_update(work_day_id: int):
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM work_days WHERE id = ?", (work_day_id,)).fetchone()
    if not exists:
        abort(404)
    d = parse_day_date_value(request.form.get("day_date"))
    note = (request.form.get("note") or "").strip()
    next_steps = (request.form.get("next_steps") or "").strip()
    questions = (request.form.get("questions") or "").strip()
    if not d:
        return redirect(url_for("work_day_view", work_day_id=work_day_id))
    existing = get_work_day_id_for_date(conn, d)
    if existing is not None and existing != work_day_id:
        conn.execute(
            """
            UPDATE work_days SET note = ?, next_steps = ?, questions = ? WHERE id = ?
            """,
            (note, next_steps, questions, work_day_id),
        )
        conn.commit()
        _merge_work_days(conn, existing, work_day_id)
        conn.commit()
        return redirect(url_for("work_day_view", work_day_id=existing))
    conn.execute(
        """
        UPDATE work_days
        SET title = ?, day_date = ?, note = ?, next_steps = ?, questions = ?
        WHERE id = ?
        """,
        (format_work_day_title(d), d.isoformat(), note, next_steps, questions, work_day_id),
    )
    conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.post("/work-days/<int:work_day_id>/delete")
def work_day_delete(work_day_id: int):
    conn = get_db()
    conn.execute("DELETE FROM work_sessions WHERE work_day_id = ?", (work_day_id,))
    conn.execute("DELETE FROM tasks WHERE work_day_id = ?", (work_day_id,))
    conn.execute("DELETE FROM work_days WHERE id = ?", (work_day_id,))
    conn.commit()
    referrer = request.referrer
    if referrer and referrer.startswith(request.host_url):
        return redirect(referrer)
    return redirect(url_for("work_days_home"))


@app.get("/tasks")
def tasks_redirect():
    return redirect(url_for("work_days_home"))


@app.post("/active-tasks")
def create_active_task():
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect_back()
    conn = get_db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM active_tasks").fetchone()[0]
    conn.execute(
        "INSERT INTO active_tasks (name, sort_order, created_at) VALUES (?, ?, ?)",
        (name, max_order + 1, iso(utc_now())),
    )
    conn.commit()
    return redirect_back()


@app.post("/active-tasks/<int:active_task_id>/update")
def update_active_task(active_task_id: int):
    name = (request.form.get("name") or "").strip()
    conn = get_db()
    row = conn.execute("SELECT 1 FROM active_tasks WHERE id = ?", (active_task_id,)).fetchone()
    if not row:
        abort(404)
    if not name:
        conn.execute("DELETE FROM active_tasks WHERE id = ?", (active_task_id,))
    else:
        conn.execute("UPDATE active_tasks SET name = ? WHERE id = ?", (name, active_task_id))
    conn.commit()
    return redirect_back()


@app.post("/active-tasks/<int:active_task_id>/delete")
def delete_active_task(active_task_id: int):
    conn = get_db()
    conn.execute("DELETE FROM active_tasks WHERE id = ?", (active_task_id,))
    conn.commit()
    return redirect_back()


@app.post("/work-days/<int:work_day_id>/tasks/from-active/<int:active_task_id>")
def add_active_task_to_day(work_day_id: int, active_task_id: int):
    conn = get_db()
    wd = conn.execute("SELECT 1 FROM work_days WHERE id = ?", (work_day_id,)).fetchone()
    active_task = conn.execute(
        "SELECT name FROM active_tasks WHERE id = ?",
        (active_task_id,),
    ).fetchone()
    if not wd or not active_task:
        abort(404)
    existing = conn.execute(
        """
        SELECT 1 FROM tasks
        WHERE work_day_id = ? AND name = ? COLLATE NOCASE
        """,
        (work_day_id, active_task["name"]),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO tasks (work_day_id, name, created_at) VALUES (?, ?, ?)",
            (work_day_id, active_task["name"], iso(utc_now())),
        )
        conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.post("/work-days/<int:work_day_id>/tasks")
def create_task(work_day_id: int):
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect(url_for("work_day_view", work_day_id=work_day_id))
    conn = get_db()
    wd = conn.execute("SELECT 1 FROM work_days WHERE id = ?", (work_day_id,)).fetchone()
    if not wd:
        abort(404)
    existing = conn.execute(
        """
        SELECT 1 FROM tasks
        WHERE work_day_id = ? AND name = ? COLLATE NOCASE
        """,
        (work_day_id, name),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO tasks (work_day_id, name, created_at) VALUES (?, ?, ?)",
            (work_day_id, name, iso(utc_now())),
        )
    conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.post("/work-days/<int:work_day_id>/tasks/<int:task_id>/delete")
def delete_task(work_day_id: int, task_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE id = ? AND work_day_id = ?",
        (task_id, work_day_id),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.post("/work-days/<int:work_day_id>/tasks/<int:task_id>/start")
def start_task(work_day_id: int, task_id: int):
    conn = get_db()
    wd = conn.execute("SELECT 1 FROM work_days WHERE id = ?", (work_day_id,)).fetchone()
    tk = conn.execute(
        "SELECT 1 FROM tasks WHERE id = ? AND work_day_id = ?",
        (task_id, work_day_id),
    ).fetchone()
    if not wd or not tk:
        return redirect(url_for("work_days_home"))
    stop_all_open_sessions(conn)
    conn.execute(
        """
        INSERT INTO work_sessions (task_id, work_day_id, started_at)
        VALUES (?, ?, ?)
        """,
        (task_id, work_day_id, iso(utc_now())),
    )
    conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.post("/work-days/<int:work_day_id>/tasks/<int:task_id>/stop")
def stop_task(work_day_id: int, task_id: int):
    conn = get_db()
    cur = active_session(conn)
    if cur and cur["task_id"] == task_id and cur.get("work_day_id") == work_day_id:
        conn.execute(
            "UPDATE work_sessions SET ended_at = ? WHERE id = ?",
            (iso(utc_now()), cur["id"]),
        )
        conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.post("/work-days/<int:work_day_id>/timer/stop")
def stop_timer(work_day_id: int):
    conn = get_db()
    cur = active_session(conn)
    if cur and cur.get("work_day_id") == work_day_id:
        conn.execute(
            "UPDATE work_sessions SET ended_at = ? WHERE id = ?",
            (iso(utc_now()), cur["id"]),
        )
        conn.commit()
    return redirect(url_for("work_day_view", work_day_id=work_day_id))


@app.get("/todo")
def todo_home():
    conn = get_db()
    projects = list_todo_projects(conn)
    return render_template("todo_projects.html", projects=projects, fmt_created=fmt_created)


@app.post("/todo/projects")
def todo_create_project():
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    if not title:
        return redirect(url_for("todo_home"))
    conn = get_db()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM todo_projects"
    ).fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO todo_projects (title, note, sort_order, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (title, note, max_order + 1, iso(utc_now())),
    )
    project_id = cur.lastrowid
    create_default_todo_columns(conn, project_id)
    conn.commit()
    return redirect(url_for("todo_project_view", project_id=project_id))


@app.get("/todo/projects/<int:project_id>")
def todo_project_view(project_id: int):
    conn = get_db()
    board = get_todo_board(conn, project_id)
    if not board:
        abort(404)
    return render_template(
        "todo_board.html",
        project=board["project"],
        columns=board["columns"],
        fmt_created=fmt_created,
    )


@app.post("/todo/projects/<int:project_id>/update")
def todo_update_project(project_id: int):
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM todo_projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not exists:
        abort(404)
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    if not title:
        return redirect(url_for("todo_project_view", project_id=project_id))
    conn.execute(
        "UPDATE todo_projects SET title = ?, note = ? WHERE id = ?",
        (title, note, project_id),
    )
    conn.commit()
    return redirect(url_for("todo_project_view", project_id=project_id))


@app.post("/todo/projects/<int:project_id>/delete")
def todo_delete_project(project_id: int):
    conn = get_db()
    cols = conn.execute(
        "SELECT id FROM todo_columns WHERE project_id = ?", (project_id,)
    ).fetchall()
    for col in cols:
        conn.execute("DELETE FROM todo_cards WHERE column_id = ?", (col["id"],))
    conn.execute("DELETE FROM todo_columns WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM todo_projects WHERE id = ?", (project_id,))
    conn.commit()
    return redirect(url_for("todo_home"))


@app.post("/todo/columns/<int:column_id>/cards")
def todo_create_card(column_id: int):
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    conn = get_db()
    col = conn.execute(
        "SELECT id, project_id FROM todo_columns WHERE id = ?",
        (column_id,),
    ).fetchone()
    if not col:
        abort(404)
    if not title:
        return redirect(url_for("todo_project_view", project_id=col["project_id"]))
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM todo_cards WHERE column_id = ?",
        (column_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO todo_cards (column_id, title, note, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (column_id, title, note, max_order + 1, iso(utc_now())),
    )
    conn.commit()
    return redirect(url_for("todo_project_view", project_id=col["project_id"]))


@app.post("/todo/cards/<int:card_id>/update")
def todo_update_card(card_id: int):
    title = (request.form.get("title") or "").strip()
    note = (request.form.get("note") or "").strip()
    conn = get_db()
    card = conn.execute(
        """
        SELECT c.id, col.project_id
        FROM todo_cards c
        JOIN todo_columns col ON col.id = c.column_id
        WHERE c.id = ?
        """,
        (card_id,),
    ).fetchone()
    if not card:
        abort(404)
    if not title:
        return redirect(url_for("todo_project_view", project_id=card["project_id"]))
    conn.execute(
        "UPDATE todo_cards SET title = ?, note = ? WHERE id = ?",
        (title, note, card_id),
    )
    conn.commit()
    return redirect(url_for("todo_project_view", project_id=card["project_id"]))


@app.post("/todo/cards/<int:card_id>/delete")
def todo_delete_card(card_id: int):
    conn = get_db()
    card = conn.execute(
        """
        SELECT c.id, col.project_id
        FROM todo_cards c
        JOIN todo_columns col ON col.id = c.column_id
        WHERE c.id = ?
        """,
        (card_id,),
    ).fetchone()
    if not card:
        abort(404)
    conn.execute("DELETE FROM todo_cards WHERE id = ?", (card_id,))
    conn.commit()
    return redirect(url_for("todo_project_view", project_id=card["project_id"]))


def _reindex_todo_column(conn, column_id: int, ordered_ids: list[int]):
    for idx, cid in enumerate(ordered_ids):
        conn.execute(
            "UPDATE todo_cards SET column_id = ?, sort_order = ? WHERE id = ?",
            (column_id, idx, cid),
        )


@app.post("/todo/cards/move")
def todo_move_card():
    data = request.get_json(silent=True) or {}
    card_id = data.get("card_id")
    column_id = data.get("column_id")
    order = data.get("order", 0)
    try:
        card_id = int(card_id)
        column_id = int(column_id)
        order = max(0, int(order))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "invalid payload"}), 400

    conn = get_db()
    card = conn.execute(
        "SELECT id, column_id FROM todo_cards WHERE id = ?",
        (card_id,),
    ).fetchone()
    new_col = conn.execute(
        "SELECT id FROM todo_columns WHERE id = ?",
        (column_id,),
    ).fetchone()
    if not card or not new_col:
        return jsonify({"success": False, "error": "not found"}), 404

    old_column_id = card["column_id"]
    old_ids = [
        r["id"]
        for r in conn.execute(
            """
            SELECT id FROM todo_cards
            WHERE column_id = ? AND id != ?
            ORDER BY sort_order ASC, id ASC
            """,
            (old_column_id, card_id),
        )
    ]
    if old_column_id == column_id:
        order = min(order, len(old_ids))
        new_ids = old_ids[:]
        new_ids.insert(order, card_id)
        _reindex_todo_column(conn, column_id, new_ids)
    else:
        _reindex_todo_column(conn, old_column_id, old_ids)
        target_ids = [
            r["id"]
            for r in conn.execute(
                """
                SELECT id FROM todo_cards
                WHERE column_id = ? AND id != ?
                ORDER BY sort_order ASC, id ASC
                """,
                (column_id, card_id),
            )
        ]
        order = min(order, len(target_ids))
        target_ids.insert(order, card_id)
        _reindex_todo_column(conn, column_id, target_ids)

    conn.commit()
    return jsonify({"success": True})


@app.get("/api/task-names")
def api_task_names():
    q = (request.args.get("q") or "").strip()
    conn = get_db()
    if not q:
        rows = conn.execute(
            """
            SELECT name, COUNT(*) AS cnt
            FROM tasks
            GROUP BY name COLLATE NOCASE
            ORDER BY cnt DESC, name COLLATE NOCASE ASC
            LIMIT 20
            """
        ).fetchall()
    else:
        like = f"%{q}%"
        prefix = f"{q}%"
        rows = conn.execute(
            """
            SELECT name, COUNT(*) AS cnt
            FROM tasks
            WHERE name LIKE ? COLLATE NOCASE
            GROUP BY name COLLATE NOCASE
            ORDER BY
              CASE WHEN name LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
              cnt DESC,
              name COLLATE NOCASE ASC
            LIMIT 20
            """,
            (like, prefix),
        ).fetchall()
    return jsonify([r["name"] for r in rows])


@app.get("/api/state")
def api_state():
    work_day_id = request.args.get("work_day_id", type=int)
    conn = get_db()
    active = active_session(conn)
    tasks = []
    total = 0.0
    if not work_day_id:
        return jsonify(
            {
                "active": active,
                "tasks": [],
                "total_seconds": 0.0,
                "server_now": iso(utc_now()),
            }
        )
    for t in conn.execute(
        """
        SELECT id, name FROM tasks
        WHERE work_day_id = ?
        ORDER BY name COLLATE NOCASE
        """,
        (work_day_id,),
    ):
        sec = 0.0
        cur = conn.execute(
            """
            SELECT started_at, ended_at FROM work_sessions
            WHERE task_id = ? AND work_day_id = ?
            """,
            (t["id"], work_day_id),
        )
        for r in cur:
            sec += session_duration_seconds(r["started_at"], r["ended_at"])
        tasks.append({"id": t["id"], "name": t["name"], "seconds": sec})
        total += sec
    payload = {
        "active": active,
        "tasks": tasks,
        "total_seconds": total,
        "server_now": iso(utc_now()),
    }
    return jsonify(payload)


def main():
    import os

    init_db()
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7777"))
    app.run(debug=debug, host=host, port=port)


if __name__ == "__main__":
    main()
