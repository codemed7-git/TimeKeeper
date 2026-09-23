import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

DATABASE = Path(__file__).parent / "timekeeper.db"
TIMER_SOUNDS_DIR = Path(__file__).parent / "static" / "timer_sounds"
CUSTOM_SOUNDS_DIR = TIMER_SOUNDS_DIR / "custom"
MAX_CUSTOM_SOUND_BYTES = 5 * 1024 * 1024
ALLOWED_SOUND_EXT = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".webm"}

BUILTIN_TIMER_SOUNDS = [
    ("01_pik", "Пик"),
    ("02_dvoynoy", "Двойной пик"),
    ("03_troynoy", "Тройной"),
    ("04_zvonok", "Звонок"),
    ("05_kolokolchik", "Колокольчик"),
    ("06_cifrovoy", "Цифровой"),
    ("07_puls", "Пульс"),
    ("08_arpedzhio", "Арпеджио"),
    ("09_sirena", "Сирена"),
    ("10_vverh", "Вверх"),
    ("11_vniz", "Вниз"),
    ("12_ksilofon", "Ксилофон"),
    ("13_chasy", "Часы"),
    ("14_myagkiy", "Мягкий"),
    ("15_srochnyy", "Срочный"),
]


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
    """Apply schema migrations only. Does not seed demo/sample rows.

    Legacy transforms may rewrite existing user rows (e.g. orphan sessions);
    a fresh empty database stays empty after migrate.
    """
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
    _migrate_timer(conn)
    _migrate_knowledge(conn)
    _migrate_tracker(conn)
    _migrate_networking(conn)
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
            sort_order INTEGER NOT NULL DEFAULT 0,
            collapsed INTEGER NOT NULL DEFAULT 0
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
    if "collapsed" not in _table_columns(conn, "todo_columns"):
        conn.execute(
            "ALTER TABLE todo_columns ADD COLUMN collapsed INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    if "archived" not in _table_columns(conn, "todo_projects"):
        conn.execute(
            "ALTER TABLE todo_projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    if "parent_card_id" not in _table_columns(conn, "todo_columns"):
        conn.execute(
            "ALTER TABLE todo_columns ADD COLUMN parent_card_id INTEGER REFERENCES todo_cards(id) ON DELETE CASCADE"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_todo_columns_parent_card ON todo_columns(parent_card_id)"
        )
        conn.commit()


def _migrate_timer(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS timer_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            duration_seconds INTEGER NOT NULL,
            remaining_seconds INTEGER NOT NULL,
            running_since TEXT,
            ended_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_timer_runs_ended ON timer_runs(ended_at);
        """
    )
    conn.execute("DROP TABLE IF EXISTS timer_sessions")
    conn.execute("DROP TABLE IF EXISTS timer_tasks")
    conn.commit()
    _migrate_timer_sound_fields(conn)
    CUSTOM_SOUNDS_DIR.mkdir(parents=True, exist_ok=True)


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^|\]]*)?(?:\|[^\]]+)?\]\]")


def _migrate_knowledge(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            title_norm TEXT NOT NULL UNIQUE,
            body TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES knowledge_notes(id) ON DELETE CASCADE,
            target_title TEXT NOT NULL,
            target_title_norm TEXT NOT NULL,
            target_id INTEGER REFERENCES knowledge_notes(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_links_source ON knowledge_links(source_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_links_target ON knowledge_links(target_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_links_target_norm ON knowledge_links(target_title_norm);
        """
    )
    conn.commit()


def _migrate_tracker(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tracker_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            icon TEXT NOT NULL DEFAULT '✓',
            color TEXT NOT NULL DEFAULT '#5b8cff',
            schedule_type TEXT NOT NULL DEFAULT 'daily',
            weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
            interval_days INTEGER NOT NULL DEFAULT 1,
            weekly_target INTEGER NOT NULL DEFAULT 1,
            target_value INTEGER NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT 'раз',
            start_date TEXT NOT NULL,
            due_date TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracker_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES tracker_items(id) ON DELETE CASCADE,
            entry_date TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(item_id, entry_date)
        );
        CREATE INDEX IF NOT EXISTS idx_tracker_items_active
            ON tracker_items(archived, sort_order);
        CREATE INDEX IF NOT EXISTS idx_tracker_entries_item_date
            ON tracker_entries(item_id, entry_date);
        """
    )
    conn.commit()
    tracker_cols = _table_columns(conn, "tracker_items")
    if "task_type" not in tracker_cols:
        conn.execute(
            "ALTER TABLE tracker_items ADD COLUMN task_type TEXT NOT NULL DEFAULT 'routine'"
        )
    if "budget_seconds" not in tracker_cols:
        conn.execute(
            "ALTER TABLE tracker_items ADD COLUMN budget_seconds INTEGER NOT NULL DEFAULT 0"
        )
    if "running_since" not in tracker_cols:
        conn.execute("ALTER TABLE tracker_items ADD COLUMN running_since TEXT")
    conn.commit()


def normalize_note_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip()).casefold()


def extract_wikilinks(body: str) -> list[str]:
    seen: set[str] = set()
    titles: list[str] = []
    for m in WIKILINK_RE.finditer(body or ""):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        norm = normalize_note_title(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        titles.append(re.sub(r"\s+", " ", raw))
    return titles


def rebuild_note_links(conn, note_id: int, body: str) -> None:
    conn.execute("DELETE FROM knowledge_links WHERE source_id = ?", (note_id,))
    for target_title in extract_wikilinks(body):
        target_norm = normalize_note_title(target_title)
        row = conn.execute(
            "SELECT id FROM knowledge_notes WHERE title_norm = ?",
            (target_norm,),
        ).fetchone()
        target_id = row["id"] if row else None
        if target_id == note_id:
            continue
        conn.execute(
            """
            INSERT INTO knowledge_links (source_id, target_title, target_title_norm, target_id)
            VALUES (?, ?, ?, ?)
            """,
            (note_id, target_title, target_norm, target_id),
        )


def resolve_incoming_links(conn, note_id: int, title_norm: str) -> None:
    conn.execute(
        """
        UPDATE knowledge_links
        SET target_id = ?
        WHERE target_title_norm = ? AND (target_id IS NULL OR target_id = ?)
        """,
        (note_id, title_norm, note_id),
    )


def list_knowledge_notes(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.body, n.created_at, n.updated_at,
               (SELECT COUNT(*) FROM knowledge_links l WHERE l.source_id = n.id) AS out_count,
               (SELECT COUNT(*) FROM knowledge_links l WHERE l.target_id = n.id) AS in_count
        FROM knowledge_notes n
        ORDER BY n.updated_at DESC, n.id DESC
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "body": r["body"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "out_count": r["out_count"],
            "in_count": r["in_count"],
        }
        for r in rows
    ]


def get_knowledge_note(conn, note_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, title, title_norm, body, created_at, updated_at
        FROM knowledge_notes WHERE id = ?
        """,
        (note_id,),
    ).fetchone()
    if not row:
        return None
    outgoing = conn.execute(
        """
        SELECT target_title, target_id
        FROM knowledge_links
        WHERE source_id = ?
        ORDER BY target_title COLLATE NOCASE
        """,
        (note_id,),
    ).fetchall()
    incoming = conn.execute(
        """
        SELECT n.id, n.title
        FROM knowledge_links l
        JOIN knowledge_notes n ON n.id = l.source_id
        WHERE l.target_id = ?
        ORDER BY n.title COLLATE NOCASE
        """,
        (note_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "title": row["title"],
        "title_norm": row["title_norm"],
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "outgoing": [
            {"title": r["target_title"], "id": r["target_id"]} for r in outgoing
        ],
        "incoming": [{"id": r["id"], "title": r["title"]} for r in incoming],
    }


def build_knowledge_graph(conn) -> dict:
    notes = conn.execute(
        "SELECT id, title, title_norm FROM knowledge_notes ORDER BY title COLLATE NOCASE"
    ).fetchall()
    links = conn.execute(
        """
        SELECT source_id, target_id, target_title, target_title_norm
        FROM knowledge_links
        """
    ).fetchall()

    nodes: list[dict] = []
    node_ids: set[str] = set()
    for n in notes:
        nid = f"n:{n['id']}"
        node_ids.add(nid)
        nodes.append(
            {
                "id": nid,
                "note_id": n["id"],
                "title": n["title"],
                "resolved": True,
            }
        )

    edges: list[dict] = []
    unresolved_norms: dict[str, str] = {}
    for link in links:
        source = f"n:{link['source_id']}"
        if link["target_id"]:
            target = f"n:{link['target_id']}"
        else:
            target = f"u:{link['target_title_norm']}"
            unresolved_norms[link["target_title_norm"]] = link["target_title"]
        if source not in node_ids:
            continue
        edges.append({"source": source, "target": target})

    for norm, title in unresolved_norms.items():
        uid = f"u:{norm}"
        if uid in node_ids:
            continue
        node_ids.add(uid)
        nodes.append(
            {
                "id": uid,
                "note_id": None,
                "title": title,
                "resolved": False,
            }
        )

    return {"nodes": nodes, "edges": edges}


def _allocate_unique_note_title(base: str, used_norms: set[str], suffix: str | None = None) -> str:
    base = re.sub(r"\s+", " ", (base or "").strip()) or "Без названия"
    candidates = [base]
    if suffix:
        candidates.append(f"{base} ({suffix})")
    for title in candidates:
        norm = normalize_note_title(title)
        if norm and norm not in used_norms:
            used_norms.add(norm)
            return title
    stem = candidates[-1]
    i = 2
    while True:
        title = f"{stem} {i}"
        norm = normalize_note_title(title)
        if norm not in used_norms:
            used_norms.add(norm)
            return title
        i += 1


def upsert_knowledge_note(conn, title: str, body: str, created_at: str | None = None) -> int:
    title = re.sub(r"\s+", " ", (title or "").strip())
    title_norm = normalize_note_title(title)
    now = iso(utc_now())
    created = created_at or now
    row = conn.execute(
        "SELECT id FROM knowledge_notes WHERE title_norm = ?",
        (title_norm,),
    ).fetchone()
    if row:
        note_id = row["id"]
        conn.execute(
            """
            UPDATE knowledge_notes
            SET title = ?, body = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, body, now, note_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO knowledge_notes (title, title_norm, body, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (title, title_norm, body, created, now),
        )
        note_id = cur.lastrowid
    rebuild_note_links(conn, note_id, body)
    resolve_incoming_links(conn, note_id, title_norm)
    return note_id


def import_knowledge_from_todo(conn) -> dict:
    """Create/update knowledge notes from TO-DO projects and cards with [[wikilinks]]."""
    projects = conn.execute(
        """
        SELECT id, title, note, created_at, sort_order
        FROM todo_projects
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()

    # Track titles claimed by this import so cards/projects don't collide.
    # Existing notes with the same title are upserted (re-import safe).
    used_norms: set[str] = set()

    hub_title = "TO-DO"
    used_norms.add(normalize_note_title(hub_title))

    project_titles: dict[int, str] = {}
    for p in projects:
        title = _allocate_unique_note_title(p["title"], used_norms)
        project_titles[p["id"]] = title

    card_titles: dict[int, str] = {}
    boards: list[dict] = []

    for p in projects:
        columns = []
        for col in conn.execute(
            """
            SELECT id, title, color_key, sort_order
            FROM todo_columns
            WHERE project_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (p["id"],),
        ):
            cards = []
            for card in conn.execute(
                """
                SELECT id, title, note, created_at, sort_order
                FROM todo_cards
                WHERE column_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (col["id"],),
            ):
                note_title = _allocate_unique_note_title(
                    card["title"], used_norms, suffix=project_titles[p["id"]]
                )
                card_titles[card["id"]] = note_title
                cards.append(dict(card))
            columns.append({**dict(col), "cards": cards})
        boards.append({"project": dict(p), "columns": columns})

    created_or_updated = 0

    for board in boards:
        p = board["project"]
        project_title = project_titles[p["id"]]
        parts = []
        note = (p.get("note") or "").strip()
        if note:
            parts.append(note)
        parts.append(f"Источник: TO-DO · проект «{p['title']}»")
        parts.append("")
        for col in board["columns"]:
            if not col["cards"]:
                continue
            parts.append(f"### {col['title']}")
            for card in col["cards"]:
                parts.append(f"- [[{card_titles[card['id']]}]]")
            parts.append("")
        body = "\n".join(parts).strip() + "\n"
        upsert_knowledge_note(conn, project_title, body, created_at=p.get("created_at"))
        created_or_updated += 1

        for col in board["columns"]:
            for card in col["cards"]:
                card_parts = [
                    f"Проект: [[{project_title}]]",
                    f"Статус: {col['title']}",
                ]
                card_note = (card.get("note") or "").strip()
                if card_note:
                    card_parts.append("")
                    card_parts.append(card_note)
                card_body = "\n".join(card_parts).strip() + "\n"
                upsert_knowledge_note(
                    conn,
                    card_titles[card["id"]],
                    card_body,
                    created_at=card.get("created_at"),
                )
                created_or_updated += 1

    hub_parts = ["Проекты из раздела TO-DO:", ""]
    for p in projects:
        hub_parts.append(f"- [[{project_titles[p['id']]}]]")
    if not projects:
        hub_parts.append("_Пока нет проектов._")
    upsert_knowledge_note(conn, hub_title, "\n".join(hub_parts).strip() + "\n")
    created_or_updated += 1

    conn.commit()
    return {
        "projects": len(projects),
        "cards": len(card_titles),
        "notes": created_or_updated,
        "graph": build_knowledge_graph(conn),
    }


def _migrate_timer_sound_fields(conn):
    cols = _table_columns(conn, "timer_runs")
    if "sound_key" not in cols:
        conn.execute("ALTER TABLE timer_runs ADD COLUMN sound_key TEXT NOT NULL DEFAULT ''")
    if "sound_seconds" not in cols:
        conn.execute("ALTER TABLE timer_runs ADD COLUMN sound_seconds INTEGER NOT NULL DEFAULT 0")
    if "sound_volume" not in cols:
        conn.execute("ALTER TABLE timer_runs ADD COLUMN sound_volume INTEGER NOT NULL DEFAULT 80")
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


def list_todo_projects(conn, include_archived: bool = False) -> list[dict]:
    sql = """
        SELECT p.id, p.title, p.note, p.created_at, p.archived,
               (SELECT COUNT(*) FROM todo_columns c WHERE c.project_id = p.id) AS column_count,
               (
                   SELECT COUNT(*) FROM todo_cards card
                   JOIN todo_columns c ON c.id = card.column_id
                   WHERE c.project_id = p.id
               ) AS card_count
        FROM todo_projects p
    """
    sql += "WHERE p.archived = 1 " if include_archived else "WHERE p.archived = 0 "
    sql += "ORDER BY p.sort_order ASC, p.id ASC"
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def count_archived_todo_projects(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM todo_projects WHERE archived = 1"
    ).fetchone()[0]


def _todo_card_with_subtasks(conn, card_row) -> dict:
    card = dict(card_row)
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM todo_columns sc WHERE sc.parent_card_id = ?) AS sub_columns,
            (
                SELECT COUNT(*) FROM todo_cards sub
                JOIN todo_columns sc ON sc.id = sub.column_id
                WHERE sc.parent_card_id = ?
            ) AS sub_total,
            (
                SELECT COUNT(*) FROM todo_cards sub
                JOIN todo_columns sc ON sc.id = sub.column_id
                WHERE sc.parent_card_id = ? AND sc.sort_order = (
                    SELECT MAX(sort_order) FROM todo_columns WHERE parent_card_id = ?
                )
            ) AS sub_done
        """,
        (card["id"], card["id"], card["id"], card["id"]),
    ).fetchone()
    card["has_subboard"] = bool(row["sub_columns"])
    card["subtask_total"] = row["sub_total"] or 0
    card["subtask_done"] = row["sub_done"] or 0 if row["sub_columns"] else 0
    return card


def _todo_columns_with_cards(conn, filter_sql: str, filter_param) -> list[dict]:
    columns = []
    for col in conn.execute(
        f"""
        SELECT id, title, color_key, sort_order, collapsed
        FROM todo_columns
        WHERE {filter_sql}
        ORDER BY sort_order ASC, id ASC
        """,
        (filter_param,),
    ):
        cards = [
            _todo_card_with_subtasks(conn, c)
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
                "collapsed": bool(col["collapsed"]),
                "cards": cards,
            }
        )
    return columns


def get_todo_board(conn, project_id: int) -> dict | None:
    project = conn.execute(
        "SELECT id, title, note, created_at FROM todo_projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if not project:
        return None
    columns = _todo_columns_with_cards(
        conn, "project_id = ? AND parent_card_id IS NULL", project_id
    )
    return {"project": dict(project), "columns": columns}


def ensure_card_subboard_columns(conn, card_id: int, project_id: int) -> None:
    existing = conn.execute(
        "SELECT COUNT(*) FROM todo_columns WHERE parent_card_id = ?", (card_id,)
    ).fetchone()[0]
    if existing:
        return
    parent_cols = conn.execute(
        """
        SELECT title, color_key, sort_order FROM todo_columns
        WHERE project_id = ? AND parent_card_id IS NULL
        ORDER BY sort_order ASC, id ASC
        """,
        (project_id,),
    ).fetchall()
    for pc in parent_cols:
        conn.execute(
            """
            INSERT INTO todo_columns (project_id, title, color_key, sort_order, parent_card_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, pc["title"], pc["color_key"], pc["sort_order"], card_id),
        )
    conn.commit()


def get_card_subboard(conn, card_id: int) -> dict | None:
    card = conn.execute(
        """
        SELECT c.id, c.title, c.note, col.project_id
        FROM todo_cards c
        JOIN todo_columns col ON col.id = c.column_id
        WHERE c.id = ?
        """,
        (card_id,),
    ).fetchone()
    if not card:
        return None
    columns = _todo_columns_with_cards(conn, "parent_card_id = ?", card_id)
    return {"card": dict(card), "columns": columns}


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
        CREATE TABLE IF NOT EXISTS timer_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            duration_seconds INTEGER NOT NULL,
            remaining_seconds INTEGER NOT NULL,
            running_since TEXT,
            ended_at TEXT,
            created_at TEXT NOT NULL,
            sound_key TEXT NOT NULL DEFAULT '',
            sound_seconds INTEGER NOT NULL DEFAULT 0,
            sound_volume INTEGER NOT NULL DEFAULT 80
        );
        CREATE TABLE IF NOT EXISTS knowledge_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            title_norm TEXT NOT NULL UNIQUE,
            body TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES knowledge_notes(id) ON DELETE CASCADE,
            target_title TEXT NOT NULL,
            target_title_norm TEXT NOT NULL,
            target_id INTEGER REFERENCES knowledge_notes(id) ON DELETE SET NULL
        );
        """
    )
    migrate_db(db)
    db.close()


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "timekeeper-local-dev-key"
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
        "timer_active": active_timer_run(conn),
        "fmt_duration": fmt_duration,
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


def parse_timer_duration(form) -> int | None:
    def _num(key: str) -> int:
        raw = (form.get(key) or "").strip()
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    total = _num("hours") * 3600 + _num("minutes") * 60 + _num("seconds")
    preset = (form.get("preset_seconds") or "").strip()
    if preset.isdigit():
        total = max(total, int(preset))
    if total <= 0:
        return None
    return min(total, 24 * 3600)


def list_custom_timer_sounds() -> list[dict]:
    CUSTOM_SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(CUSTOM_SOUNDS_DIR.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in ALLOWED_SOUND_EXT:
            continue
        items.append({"key": f"custom:{p.name}", "title": p.stem, "filename": p.name})
    return items


def timer_sound_file(sound_key: str) -> Path | None:
    key = (sound_key or "").strip()
    if not key or key == "none":
        return None
    if key.startswith("custom:"):
        name = Path(key.split(":", 1)[1]).name
        if not name:
            return None
        path = (CUSTOM_SOUNDS_DIR / name).resolve()
        try:
            path.relative_to(CUSTOM_SOUNDS_DIR.resolve())
        except ValueError:
            return None
        if path.is_file() and path.suffix.lower() in ALLOWED_SOUND_EXT:
            return path
        return None
    allowed = {item[0] for item in BUILTIN_TIMER_SOUNDS}
    if key not in allowed:
        return None
    path = TIMER_SOUNDS_DIR / f"{key}.wav"
    return path if path.is_file() else None


def timer_sound_url(sound_key: str) -> str:
    path = timer_sound_file(sound_key)
    if not path:
        return ""
    rel = path.relative_to(Path(__file__).parent / "static").as_posix()
    return url_for("static", filename=rel)


def save_custom_timer_sound(uploaded) -> str | None:
    raw_name = uploaded.filename or "signal.wav"
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_SOUND_EXT:
        return None
    filename = secure_filename(raw_name)
    if not filename or filename.startswith("."):
        filename = f"signal{ext}"
    data = uploaded.read(MAX_CUSTOM_SOUND_BYTES + 1)
    if not data or len(data) > MAX_CUSTOM_SOUND_BYTES:
        return None
    CUSTOM_SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = CUSTOM_SOUNDS_DIR / filename
    stem = Path(filename).stem
    n = 1
    while dest.exists():
        dest = CUSTOM_SOUNDS_DIR / f"{stem}_{n}{ext}"
        n += 1
    dest.write_bytes(data)
    return dest.name


def parse_timer_sound(form, files) -> tuple[str, int, int]:
    key = (form.get("sound_key") or "").strip()
    uploaded = files.get("sound_file") if files is not None else None
    if uploaded and getattr(uploaded, "filename", ""):
        saved = save_custom_timer_sound(uploaded)
        if saved:
            key = f"custom:{saved}"
    if key == "none":
        key = ""
    if key and not timer_sound_file(key):
        key = ""
    raw_sec = (form.get("sound_seconds") or "").strip()
    try:
        seconds = int(raw_sec) if raw_sec else 0
    except ValueError:
        seconds = 0
    seconds = max(0, min(60, seconds))
    raw_vol = (form.get("sound_volume") or "80").strip()
    try:
        volume = int(raw_vol)
    except ValueError:
        volume = 80
    volume = max(1, min(100, volume))
    if not key:
        seconds = 0
        volume = 80
    return key, seconds, volume


def timer_remaining(run: dict) -> float:
    rem = float(run["remaining_seconds"] or 0)
    if run.get("running_since"):
        rem -= session_duration_seconds(run["running_since"], None)
    return max(0.0, rem)


def _complete_timer_run(conn, run_id: int):
    conn.execute(
        """
        UPDATE timer_runs
        SET remaining_seconds = 0, running_since = NULL, ended_at = ?
        WHERE id = ? AND ended_at IS NULL
        """,
        (iso(utc_now()), run_id),
    )


def active_timer_run(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT id, name, duration_seconds, remaining_seconds, running_since, ended_at, created_at,
               sound_key, sound_seconds, sound_volume
        FROM timer_runs
        WHERE ended_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    run = dict(row)
    live = timer_remaining(run)
    if live <= 0:
        _complete_timer_run(conn, run["id"])
        conn.commit()
        return None
    run["remaining_live"] = round(live, 2)
    run["paused"] = not run["running_since"]
    run["sound_url"] = timer_sound_url(run.get("sound_key") or "")
    run["sound_seconds"] = int(run.get("sound_seconds") or 0)
    run["sound_volume"] = int(run.get("sound_volume") or 80)
    return run


def list_timer_history(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, duration_seconds, remaining_seconds, ended_at, created_at
        FROM timer_runs
        WHERE ended_at IS NOT NULL
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        remaining = int(r["remaining_seconds"] or 0)
        elapsed = max(0, int(r["duration_seconds"]) - remaining)
        out.append(
            {
                "id": r["id"],
                "name": r["name"] or "Таймер",
                "duration_seconds": r["duration_seconds"],
                "remaining_seconds": remaining,
                "elapsed_seconds": elapsed,
                "completed": remaining <= 0,
                "ended_at": r["ended_at"],
                "created_at": r["created_at"],
            }
        )
    return out


def cancel_open_timer_runs(conn):
    now = iso(utc_now())
    for row in conn.execute(
        """
        SELECT id, remaining_seconds, running_since
        FROM timer_runs
        WHERE ended_at IS NULL
        """
    ):
        rem = timer_remaining(dict(row))
        conn.execute(
            """
            UPDATE timer_runs
            SET remaining_seconds = ?, running_since = NULL, ended_at = ?
            WHERE id = ?
            """,
            (int(round(rem)), now, row["id"]),
        )


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


def iso_to_local_input(iso_s: str) -> str:
    return parse_iso(iso_s).astimezone().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M")


def parse_local_datetime_input(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return iso(dt)


def task_sessions_payload(conn, work_day_id: int, task_id: int) -> dict | None:
    task = conn.execute(
        "SELECT id, name FROM tasks WHERE id = ? AND work_day_id = ?",
        (task_id, work_day_id),
    ).fetchone()
    if not task:
        return None
    sessions = []
    for s in conn.execute(
        """
        SELECT id, started_at, ended_at FROM work_sessions
        WHERE task_id = ? AND work_day_id = ?
        ORDER BY started_at
        """,
        (task_id, work_day_id),
    ):
        sec = session_duration_seconds(s["started_at"], s["ended_at"])
        sessions.append(
            {
                "id": s["id"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "started_at_local": iso_to_local_input(s["started_at"]),
                "ended_at_local": iso_to_local_input(s["ended_at"]) if s["ended_at"] else "",
                "seconds": round(sec, 2),
                "duration": fmt_duration(sec),
                "is_active": s["ended_at"] is None,
            }
        )
    total_seconds = sum(x["seconds"] for x in sessions)
    return {
        "id": task["id"],
        "name": task["name"],
        "seconds": round(total_seconds, 2),
        "duration": fmt_duration(total_seconds),
        "sessions": sessions,
    }


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


def _parse_bool_flag(args, name: str, default: bool = True) -> bool:
    raw = args.get(name)
    if raw is None:
        return default
    v = str(raw).strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return default


def parse_report_export_options(args) -> dict:
    sort_by = (args.get("export_sort_by") or args.get("sort_by") or "day_date").strip()
    sort_dir = (args.get("export_sort_dir") or args.get("sort_dir") or "desc").strip().lower()
    task_sort = (args.get("task_sort") or "seconds").strip().lower()
    if sort_by not in {"day_date", "created_at", "title", "seconds", "task_count", "session_count"}:
        sort_by = "day_date"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"
    if task_sort not in {"seconds", "name"}:
        task_sort = "seconds"
    max_tasks_raw = (args.get("max_tasks") or "").strip()
    max_tasks = None
    if max_tasks_raw:
        try:
            max_tasks = max(0, int(max_tasks_raw))
        except (TypeError, ValueError):
            max_tasks = None
        if max_tasks == 0:
            max_tasks = None
    return {
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "task_sort": task_sort,
        "include_empty_days": _parse_bool_flag(args, "include_empty_days", True),
        "include_empty_tasks": _parse_bool_flag(args, "include_empty_tasks", True),
        "show_empty_fields": _parse_bool_flag(args, "show_empty_fields", True),
        "include_note": _parse_bool_flag(args, "include_note", True),
        "include_next_steps": _parse_bool_flag(args, "include_next_steps", True),
        "include_questions": _parse_bool_flag(args, "include_questions", True),
        "include_tasks": _parse_bool_flag(args, "include_tasks", True),
        "include_session_counts": _parse_bool_flag(args, "include_session_counts", True),
        "include_day_totals": _parse_bool_flag(args, "include_day_totals", True),
        "include_summary_header": _parse_bool_flag(args, "include_summary_header", True),
        "max_tasks": max_tasks,
    }


def apply_export_task_options(tasks: list, options: dict) -> list:
    out = list(tasks or [])
    if not options.get("include_empty_tasks", True):
        out = [t for t in out if (t.get("seconds") or 0) > 0]
    task_sort = options.get("task_sort") or "seconds"
    if task_sort == "name":
        out.sort(key=lambda t: (t.get("name") or "").lower())
    else:
        out.sort(key=lambda t: (-(t.get("seconds") or 0), (t.get("name") or "").lower()))
    max_tasks = options.get("max_tasks")
    if max_tasks is not None:
        out = out[:max_tasks]
    return out


def apply_export_day_options(days: list[dict], options: dict) -> list[dict]:
    out = list(days or [])
    if not options.get("include_empty_days", True):
        out = [d for d in out if (d.get("seconds") or 0) > 0]
    sort_by = options.get("sort_by") or "day_date"
    if sort_by == "created_at":
        sort_by = "day_date"
    sort_dir = options.get("sort_dir") or "desc"
    sorters = {
        "day_date": lambda d: work_day_sort_date(d).isoformat(),
        "title": lambda d: (d.get("title") or "").lower(),
        "seconds": lambda d: d.get("seconds") or 0,
        "task_count": lambda d: d.get("task_count") or 0,
        "session_count": lambda d: d.get("session_count") or 0,
    }
    key = sorters.get(sort_by, sorters["day_date"])
    out.sort(key=key, reverse=(sort_dir == "desc"))
    return out


def format_single_day_report_text(work_day: dict, tasks: list, summary: dict, options: dict | None = None) -> str:
    opts = options or parse_report_export_options({})
    lines = [
        f"Рабочий день: {fmt_day_date(work_day.get('day_date')) or work_day.get('title') or '—'}",
        "",
    ]
    if opts.get("include_day_totals", True):
        lines.append(f"Всего времени: {summary['total_duration']}")
        lines.append("")

    if opts.get("include_tasks", True):
        visible_tasks = apply_export_task_options(tasks, opts)
        lines.append(f"Задачи > {len(visible_tasks)} <:")
        for idx, t in enumerate(visible_tasks, start=1):
            if opts.get("include_session_counts", True):
                lines.append(f"{idx}. {t['name']} — {t['duration']} ({t['session_count']} сессий)")
            else:
                lines.append(f"{idx}. {t['name']} — {t['duration']}")
        lines.append("")

    show_empty = opts.get("show_empty_fields", True)

    if opts.get("include_note", True):
        bullets = format_bullet_lines(work_day.get("note") or "")
        if bullets or show_empty:
            lines.append("Итоги работы:")
            if bullets:
                lines.extend(bullets)
            elif show_empty:
                lines.append("-")
            lines.append("")

    if opts.get("include_next_steps", True):
        next_steps = format_text_block(work_day.get("next_steps") or "")
        if next_steps or show_empty:
            lines.append("Дальнейшее направление работы (активные задачи):")
            if next_steps:
                lines.extend(next_steps)
            else:
                lines.append("-")
            lines.append("")

    if opts.get("include_questions", True):
        questions = format_text_block(work_day.get("questions") or "")
        if questions or show_empty:
            lines.append("Вопросы|проблемы:")
            if questions:
                lines.extend(questions)
            else:
                lines.append("-")
            lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _md_escape_cell(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def format_single_day_report_md(work_day: dict, tasks: list, summary: dict, options: dict | None = None) -> str:
    opts = options or parse_report_export_options({})
    title = fmt_day_date(work_day.get("day_date")) or work_day.get("title") or "—"
    lines = [f"## {title}", ""]

    if opts.get("include_day_totals", True):
        lines.append(f"**Всего времени:** {summary['total_duration']}")
        lines.append("")

    if opts.get("include_tasks", True):
        visible_tasks = apply_export_task_options(tasks, opts)
        lines.append(f"### Задачи ({len(visible_tasks)})")
        lines.append("")
        if visible_tasks:
            if opts.get("include_session_counts", True):
                lines.append("| # | Задача | Время | Сессии |")
                lines.append("| -: | --- | --- | -: |")
                for idx, t in enumerate(visible_tasks, start=1):
                    lines.append(
                        f"| {idx} | {_md_escape_cell(t['name'])} | {t['duration']} | {t['session_count']} |"
                    )
            else:
                lines.append("| # | Задача | Время |")
                lines.append("| -: | --- | --- |")
                for idx, t in enumerate(visible_tasks, start=1):
                    lines.append(f"| {idx} | {_md_escape_cell(t['name'])} | {t['duration']} |")
        else:
            lines.append("_Нет задач_")
        lines.append("")

    show_empty = opts.get("show_empty_fields", True)

    if opts.get("include_note", True):
        bullets = format_bullet_lines(work_day.get("note") or "")
        if bullets or show_empty:
            lines.append("### Итоги работы")
            lines.append("")
            if bullets:
                lines.extend(bullets)
            else:
                lines.append("-")
            lines.append("")

    if opts.get("include_next_steps", True):
        next_steps = format_text_block(work_day.get("next_steps") or "")
        if next_steps or show_empty:
            lines.append("### Дальнейшее направление работы")
            lines.append("")
            if next_steps:
                for step in next_steps:
                    if step.startswith(("-", "*", "•")):
                        lines.append(f"- {step.lstrip('-*• ').strip()}")
                    else:
                        lines.append(f"- {step}")
            else:
                lines.append("-")
            lines.append("")

    if opts.get("include_questions", True):
        questions = format_text_block(work_day.get("questions") or "")
        if questions or show_empty:
            lines.append("### Вопросы / проблемы")
            lines.append("")
            if questions:
                for q in questions:
                    if q.startswith(("-", "*", "•")):
                        lines.append(f"- {q.lstrip('-*• ').strip()}")
                    else:
                        lines.append(f"- {q}")
            else:
                lines.append("-")
            lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
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


def collect_multi_work_days_report(conn, days: list[dict], filters: dict, options: dict | None = None) -> dict:
    opts = options or parse_report_export_options({})
    days = apply_export_day_options(days, opts)
    export_days = []
    for d in days:
        day_report = collect_work_day_report(conn, d["id"], opts)
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
        note = d["note"]
        next_steps = d.get("next_steps") or (day_report or {}).get("work_day", {}).get("next_steps", "")
        questions = d.get("questions") or (day_report or {}).get("work_day", {}).get("questions", "")
        if not opts.get("include_note", True):
            note = ""
        if not opts.get("include_next_steps", True):
            next_steps = ""
        if not opts.get("include_questions", True):
            questions = ""
        if not opts.get("include_tasks", True):
            tasks = []
        resolved_tasks_count = sum(1 for t in tasks if t["seconds"] > 0)
        session_count = sum(t.get("session_count") or 0 for t in tasks) if opts.get("include_tasks", True) else d["session_count"]
        item = {
            "id": d["id"],
            "title": d["title"],
            "note": note,
            "next_steps": next_steps,
            "questions": questions,
            "day_date": d.get("day_date") or "",
            "day_local": fmt_day_date(d.get("day_date")),
            "seconds": d["seconds"],
            "formatted_duration": fmt_duration(d["seconds"]),
            "task_count": len(tasks) if opts.get("include_tasks", True) else 0,
            "session_count": session_count,
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
        "options": opts,
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
    opts = report.get("options") or parse_report_export_options({})
    parts = []
    if opts.get("include_summary_header", True):
        summary = report.get("summary") or {}
        parts.append(
            "\n".join(
                [
                    "Отчёт по рабочим дням",
                    f"Дней: {summary.get('days_count', 0)}",
                    f"Всего времени: {summary.get('total_duration', '0:00')}",
                    f"Задач: {summary.get('task_count', 0)} · Сессий: {summary.get('session_count', 0)}",
                ]
            )
        )
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
                opts,
            )
        )
    return "\n\n".join(parts)


def multi_report_md_text(report: dict) -> str:
    opts = report.get("options") or parse_report_export_options({})
    parts = []
    if opts.get("include_summary_header", True):
        summary = report.get("summary") or {}
        parts.append(
            "\n".join(
                [
                    "# Отчёт по рабочим дням",
                    "",
                    f"- **Дней:** {summary.get('days_count', 0)}",
                    f"- **Всего времени:** {summary.get('total_duration', '0:00')}",
                    f"- **Задач:** {summary.get('task_count', 0)}",
                    f"- **Сессий:** {summary.get('session_count', 0)}",
                ]
            )
        )
    for d in report["days"]:
        day_report = {
            "work_day": {
                "day_date": d.get("day_date") or "",
                "title": d.get("title") or "",
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
            format_single_day_report_md(
                day_report["work_day"],
                day_report["tasks"],
                day_report["summary"],
                opts,
            )
        )
    return "\n\n---\n\n".join(parts)


def collect_work_day_report(conn, work_day_id: int, options: dict | None = None) -> dict | None:
    opts = options or parse_report_export_options({})
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
    tasks = apply_export_task_options(tasks, opts)
    note = wd["note"] or ""
    next_steps = wd["next_steps"] or ""
    questions = wd["questions"] or ""
    if not opts.get("include_note", True):
        note = ""
    if not opts.get("include_next_steps", True):
        next_steps = ""
    if not opts.get("include_questions", True):
        questions = ""
    if not opts.get("include_tasks", True):
        tasks = []
    return {
        "version": 1,
        "exported_at": iso(utc_now()),
        "options": opts,
        "work_day": {
            "id": wd["id"],
            "title": wd["title"],
            "day_date": wd["day_date"] or "",
            "note": note,
            "next_steps": next_steps,
            "questions": questions,
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
        report.get("options"),
    )


def report_md_text(report: dict) -> str:
    return format_single_day_report_md(
        report["work_day"],
        report["tasks"],
        report["summary"],
        report.get("options"),
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
    options = parse_report_export_options(request.args)
    report = collect_work_day_report(conn, work_day_id, options)
    if not report:
        abort(404)
    return jsonify(report)


@app.get("/work-days/<int:work_day_id>/report.txt")
def export_report_txt(work_day_id: int):
    conn = get_db()
    options = parse_report_export_options(request.args)
    report = collect_work_day_report(conn, work_day_id, options)
    if not report:
        abort(404)
    filename = _safe_report_filename(report["work_day"]["title"], "txt")
    body = report_txt_text(report)
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/work-days/<int:work_day_id>/report.md")
def export_report_md(work_day_id: int):
    conn = get_db()
    options = parse_report_export_options(request.args)
    report = collect_work_day_report(conn, work_day_id, options)
    if not report:
        abort(404)
    filename = _safe_report_filename(report["work_day"]["title"], "md")
    body = report_md_text(report)
    return Response(
        body,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/reports/multi-data")
def multi_report_data():
    conn = get_db()
    filters = parse_work_day_filters(request.args)
    options = parse_report_export_options(request.args)
    days = filter_and_sort_days(build_work_days_overview(conn), filters)
    return jsonify(collect_multi_work_days_report(conn, days, filters, options))


@app.get("/reports/multi.txt")
def export_multi_report_txt():
    conn = get_db()
    filters = parse_work_day_filters(request.args)
    options = parse_report_export_options(request.args)
    days = filter_and_sort_days(build_work_days_overview(conn), filters)
    report = collect_multi_work_days_report(conn, days, filters, options)
    body = multi_report_txt_text(report)
    filename = _safe_report_filename(f"work_days_{len(report.get('days') or [])}", "txt")
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/reports/multi.md")
def export_multi_report_md():
    conn = get_db()
    filters = parse_work_day_filters(request.args)
    options = parse_report_export_options(request.args)
    days = filter_and_sort_days(build_work_days_overview(conn), filters)
    report = collect_multi_work_days_report(conn, days, filters, options)
    body = multi_report_md_text(report)
    filename = _safe_report_filename(f"work_days_{len(report.get('days') or [])}", "md")
    return Response(
        body,
        mimetype="text/markdown; charset=utf-8",
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


@app.get("/work-days/<int:work_day_id>/tasks/<int:task_id>/data")
def task_data(work_day_id: int, task_id: int):
    conn = get_db()
    data = task_sessions_payload(conn, work_day_id, task_id)
    if not data:
        abort(404)
    return jsonify(data)


@app.post("/work-days/<int:work_day_id>/tasks/<int:task_id>/edit")
def task_edit(work_day_id: int, task_id: int):
    conn = get_db()
    task = conn.execute(
        "SELECT id FROM tasks WHERE id = ? AND work_day_id = ?",
        (task_id, work_day_id),
    ).fetchone()
    if not task:
        abort(404)

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Название не может быть пустым"}), 400

    duplicate = conn.execute(
        """
        SELECT id FROM tasks
        WHERE work_day_id = ? AND name = ? COLLATE NOCASE AND id != ?
        """,
        (work_day_id, name, task_id),
    ).fetchone()
    if duplicate:
        return jsonify({"error": "Задача с таким названием уже есть в этом дне"}), 400

    sessions_data = body.get("sessions") or []
    delete_ids = [int(x) for x in (body.get("delete_session_ids") or []) if str(x).isdigit()]

    active_count = sum(1 for s in sessions_data if not (s.get("ended_at") or "").strip())
    if active_count > 1:
        return jsonify({"error": "Только одна сессия может быть без времени окончания"}), 400

    for sid in delete_ids:
        row = conn.execute(
            """
            SELECT id FROM work_sessions
            WHERE id = ? AND task_id = ? AND work_day_id = ?
            """,
            (sid, task_id, work_day_id),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM work_sessions WHERE id = ?", (sid,))

    if active_count:
        stop_all_open_sessions(conn)

    for sess in sessions_data:
        sid = sess.get("id")
        if not sid:
            continue
        row = conn.execute(
            """
            SELECT id FROM work_sessions
            WHERE id = ? AND task_id = ? AND work_day_id = ?
            """,
            (sid, task_id, work_day_id),
        ).fetchone()
        if not row:
            continue
        started_local = (sess.get("started_at") or "").strip()
        ended_local = (sess.get("ended_at") or "").strip()
        if not started_local:
            return jsonify({"error": "Укажите время начала каждой сессии"}), 400
        try:
            started_at = parse_local_datetime_input(started_local)
            ended_at = parse_local_datetime_input(ended_local) if ended_local else None
        except ValueError:
            return jsonify({"error": "Некорректный формат даты или времени"}), 400
        if ended_at and parse_iso(ended_at) <= parse_iso(started_at):
            return jsonify({"error": "Время окончания должно быть позже начала"}), 400
        conn.execute(
            "UPDATE work_sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (started_at, ended_at, sid),
        )

    conn.execute("UPDATE tasks SET name = ? WHERE id = ?", (name, task_id))
    conn.commit()
    data = task_sessions_payload(conn, work_day_id, task_id)
    return jsonify({"success": True, "task": data})


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


@app.get("/timer")
def timer_home():
    conn = get_db()
    active = active_timer_run(conn)
    history = list_timer_history(conn)
    return render_template(
        "timer.html",
        active=active,
        history=history,
        fmt=fmt_duration,
        fmt_created=fmt_created,
        builtin_sounds=BUILTIN_TIMER_SOUNDS,
        custom_sounds=list_custom_timer_sounds(),
    )


@app.post("/timer/start")
def timer_start():
    name = (request.form.get("name") or "").strip()
    duration = parse_timer_duration(request.form)
    if not duration:
        return redirect(url_for("timer_home"))
    sound_key, sound_seconds, sound_volume = parse_timer_sound(request.form, request.files)
    conn = get_db()
    cancel_open_timer_runs(conn)
    now = iso(utc_now())
    conn.execute(
        """
        INSERT INTO timer_runs (
            name, duration_seconds, remaining_seconds, running_since, ended_at, created_at,
            sound_key, sound_seconds, sound_volume
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (name, duration, duration, now, now, sound_key, sound_seconds, sound_volume),
    )
    conn.commit()
    return redirect(url_for("timer_home"))


@app.post("/timer/pause")
def timer_pause():
    conn = get_db()
    cur = active_timer_run(conn)
    if cur and cur.get("running_since"):
        rem = int(round(timer_remaining(cur)))
        conn.execute(
            """
            UPDATE timer_runs
            SET remaining_seconds = ?, running_since = NULL
            WHERE id = ?
            """,
            (rem, cur["id"]),
        )
        conn.commit()
    return redirect_back("timer_home")


@app.post("/timer/resume")
def timer_resume():
    conn = get_db()
    cur = active_timer_run(conn)
    if cur and not cur.get("running_since"):
        conn.execute(
            "UPDATE timer_runs SET running_since = ? WHERE id = ?",
            (iso(utc_now()), cur["id"]),
        )
        conn.commit()
    return redirect_back("timer_home")


@app.post("/timer/stop")
def timer_stop():
    conn = get_db()
    cancel_open_timer_runs(conn)
    conn.commit()
    return redirect_back("timer_home")


@app.post("/timer/complete")
def timer_complete():
    conn = get_db()
    cur = active_timer_run(conn)
    if cur:
        _complete_timer_run(conn, cur["id"])
        conn.commit()
    wants_json = "application/json" in (request.headers.get("Accept") or "")
    if wants_json:
        return jsonify({"success": True, "active": None})
    return redirect_back("timer_home")


@app.post("/timer/runs/<int:run_id>/delete")
def timer_delete_run(run_id: int):
    conn = get_db()
    conn.execute("DELETE FROM timer_runs WHERE id = ? AND ended_at IS NOT NULL", (run_id,))
    conn.commit()
    return redirect(url_for("timer_home"))


@app.get("/todo")
def todo_home():
    conn = get_db()
    show_archived = request.args.get("archived") == "1"
    projects = list_todo_projects(conn, include_archived=show_archived)
    archived_count = count_archived_todo_projects(conn)
    return render_template(
        "todo_projects.html",
        projects=projects,
        fmt_created=fmt_created,
        show_archived=show_archived,
        archived_count=archived_count,
    )


@app.post("/todo/projects/<int:project_id>/archive")
def todo_archive_project(project_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT archived FROM todo_projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not row:
        abort(404)
    conn.execute(
        "UPDATE todo_projects SET archived = ? WHERE id = ?",
        (0 if row["archived"] else 1, project_id),
    )
    conn.commit()
    return redirect_back("todo_home")


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


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


@app.post("/todo/projects/<int:project_id>/columns")
def todo_create_column(project_id: int):
    title = (request.form.get("title") or "").strip()
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM todo_projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not exists:
        abort(404)
    if not title:
        if _wants_json():
            return jsonify({"error": "title required"}), 400
        return redirect(url_for("todo_project_view", project_id=project_id))
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM todo_columns WHERE project_id = ? AND parent_card_id IS NULL",
        (project_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO todo_columns (project_id, title, color_key, sort_order)
        VALUES (?, ?, ?, ?)
        """,
        (project_id, title, "planned", max_order + 1),
    )
    conn.commit()
    if _wants_json():
        return jsonify(
            {
                "ok": True,
                "column": {"id": cur.lastrowid, "title": title, "color_key": "planned", "collapsed": False, "cards": []},
            }
        )
    return redirect(url_for("todo_project_view", project_id=project_id))


@app.post("/todo/columns/<int:column_id>/update")
def todo_update_column(column_id: int):
    conn = get_db()
    col = conn.execute(
        "SELECT id, project_id FROM todo_columns WHERE id = ?",
        (column_id,),
    ).fetchone()
    if not col:
        abort(404)
    title = (request.form.get("title") or "").strip()
    if not title:
        if _wants_json():
            return jsonify({"error": "title required"}), 400
        return redirect(url_for("todo_project_view", project_id=col["project_id"]))
    conn.execute(
        "UPDATE todo_columns SET title = ? WHERE id = ?",
        (title, column_id),
    )
    conn.commit()
    if _wants_json():
        return jsonify({"ok": True, "column": {"id": column_id, "title": title}})
    return redirect(url_for("todo_project_view", project_id=col["project_id"]))


@app.post("/todo/columns/<int:column_id>/delete")
def todo_delete_column(column_id: int):
    conn = get_db()
    col = conn.execute(
        "SELECT id, project_id FROM todo_columns WHERE id = ?",
        (column_id,),
    ).fetchone()
    if not col:
        abort(404)
    conn.execute("DELETE FROM todo_cards WHERE column_id = ?", (column_id,))
    conn.execute("DELETE FROM todo_columns WHERE id = ?", (column_id,))
    conn.commit()
    if _wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("todo_project_view", project_id=col["project_id"]))


@app.post("/todo/columns/<int:column_id>/collapse")
def todo_collapse_column(column_id: int):
    conn = get_db()
    col = conn.execute(
        "SELECT id, project_id FROM todo_columns WHERE id = ?",
        (column_id,),
    ).fetchone()
    if not col:
        abort(404)
    data = request.get_json(silent=True)
    if data is not None and "collapsed" in data:
        collapsed = bool(data.get("collapsed"))
    else:
        collapsed = (request.form.get("collapsed") or "0").strip() == "1"
    conn.execute(
        "UPDATE todo_columns SET collapsed = ? WHERE id = ?",
        (1 if collapsed else 0, column_id),
    )
    conn.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"success": True, "collapsed": collapsed})
    return redirect(url_for("todo_project_view", project_id=col["project_id"]))


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
        if _wants_json():
            return jsonify({"error": "title required"}), 400
        return redirect(url_for("todo_project_view", project_id=col["project_id"]))
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM todo_cards WHERE column_id = ?",
        (column_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO todo_cards (column_id, title, note, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (column_id, title, note, max_order + 1, iso(utc_now())),
    )
    conn.commit()
    if _wants_json():
        card_row = conn.execute(
            "SELECT id, title, note, sort_order, created_at FROM todo_cards WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return jsonify({"ok": True, "card": _todo_card_with_subtasks(conn, card_row)})
    return redirect(url_for("todo_project_view", project_id=col["project_id"], new_card=cur.lastrowid))


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
        if _wants_json():
            return jsonify({"error": "title required"}), 400
        return redirect(url_for("todo_project_view", project_id=card["project_id"]))
    conn.execute(
        "UPDATE todo_cards SET title = ?, note = ? WHERE id = ?",
        (title, note, card_id),
    )
    conn.commit()
    if _wants_json():
        return jsonify({"ok": True, "card": {"id": card_id, "title": title, "note": note}})
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
    if _wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("todo_project_view", project_id=card["project_id"]))


@app.get("/todo/cards/<int:card_id>/subboard")
def todo_card_subboard(card_id: int):
    conn = get_db()
    card = conn.execute(
        """
        SELECT c.id, c.title, col.project_id
        FROM todo_cards c
        JOIN todo_columns col ON col.id = c.column_id
        WHERE c.id = ?
        """,
        (card_id,),
    ).fetchone()
    if not card:
        abort(404)
    ensure_card_subboard_columns(conn, card_id, card["project_id"])
    board = get_card_subboard(conn, card_id)
    return render_template(
        "_todo_subboard.html", card=board["card"], columns=board["columns"]
    )


@app.post("/todo/cards/<int:card_id>/subboard/columns")
def todo_create_subcolumn(card_id: int):
    title = (request.form.get("title") or "").strip()
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
        if _wants_json():
            return jsonify({"error": "title required"}), 400
        return redirect(url_for("todo_project_view", project_id=card["project_id"]))
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM todo_columns WHERE parent_card_id = ?",
        (card_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO todo_columns (project_id, title, color_key, sort_order, parent_card_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (card["project_id"], title, "planned", max_order + 1, card_id),
    )
    conn.commit()
    if _wants_json():
        return jsonify(
            {
                "ok": True,
                "column": {"id": cur.lastrowid, "title": title, "color_key": "planned", "collapsed": False, "cards": []},
            }
        )
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


@app.get("/api/timer-state")
def api_timer_state():
    conn = get_db()
    active = active_timer_run(conn)
    payload = None
    if active:
        payload = {
            "id": active["id"],
            "name": active["name"] or "Таймер",
            "duration_seconds": active["duration_seconds"],
            "remaining_seconds": active["remaining_seconds"],
            "remaining_live": active["remaining_live"],
            "running_since": active["running_since"],
            "paused": active["paused"],
            "sound_url": active.get("sound_url") or "",
            "sound_seconds": active.get("sound_seconds") or 0,
            "sound_volume": active.get("sound_volume") or 80,
        }
    return jsonify(
        {
            "active": payload,
            "server_now": iso(utc_now()),
        }
    )


@app.get("/knowledge")
def knowledge_home():
    conn = get_db()
    notes = list_knowledge_notes(conn)
    graph = build_knowledge_graph(conn)
    return render_template(
        "knowledge.html",
        notes=notes,
        graph=graph,
        fmt_created=fmt_created,
    )


@app.post("/knowledge/import-todo")
def knowledge_import_todo():
    conn = get_db()
    result = import_knowledge_from_todo(conn)
    wants_json = (
        request.headers.get("X-Requested-With") == "fetch"
        or "application/json" in (request.accept_mimetypes.best or "")
    )
    if wants_json:
        return jsonify(
            {
                "ok": True,
                "projects": result["projects"],
                "cards": result["cards"],
                "notes": result["notes"],
                "graph": result["graph"],
                "notes_list": list_knowledge_notes(conn),
            }
        )
    return redirect(url_for("knowledge_home"))


@app.get("/api/knowledge/graph")
def api_knowledge_graph():
    conn = get_db()
    return jsonify(build_knowledge_graph(conn))


@app.get("/api/knowledge/notes")
def api_knowledge_notes():
    conn = get_db()
    return jsonify({"notes": list_knowledge_notes(conn)})


@app.get("/api/knowledge/notes/<int:note_id>")
def api_knowledge_note(note_id: int):
    conn = get_db()
    note = get_knowledge_note(conn, note_id)
    if not note:
        abort(404)
    return jsonify(note)


@app.post("/api/knowledge/notes")
def api_knowledge_create_note():
    data = request.get_json(silent=True) or {}
    title = re.sub(r"\s+", " ", (data.get("title") or "").strip())
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    if not title:
        return jsonify({"error": "Укажите название"}), 400
    title_norm = normalize_note_title(title)
    conn = get_db()
    exists = conn.execute(
        "SELECT id FROM knowledge_notes WHERE title_norm = ?",
        (title_norm,),
    ).fetchone()
    if exists:
        return jsonify({"error": "Заметка с таким названием уже есть", "id": exists["id"]}), 409
    now = iso(utc_now())
    cur = conn.execute(
        """
        INSERT INTO knowledge_notes (title, title_norm, body, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (title, title_norm, body, now, now),
    )
    note_id = cur.lastrowid
    rebuild_note_links(conn, note_id, body)
    resolve_incoming_links(conn, note_id, title_norm)
    conn.commit()
    note = get_knowledge_note(conn, note_id)
    return jsonify({"note": note, "graph": build_knowledge_graph(conn)}), 201


@app.post("/api/knowledge/notes/<int:note_id>")
def api_knowledge_update_note(note_id: int):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    existing = conn.execute(
        "SELECT id, title_norm FROM knowledge_notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    if not existing:
        abort(404)

    title = data.get("title")
    body = data.get("body")
    updates = []
    values = []

    new_title_norm = existing["title_norm"]
    if title is not None:
        title = re.sub(r"\s+", " ", str(title).strip())
        if not title:
            return jsonify({"error": "Укажите название"}), 400
        new_title_norm = normalize_note_title(title)
        clash = conn.execute(
            "SELECT id FROM knowledge_notes WHERE title_norm = ? AND id != ?",
            (new_title_norm, note_id),
        ).fetchone()
        if clash:
            return jsonify({"error": "Заметка с таким названием уже есть", "id": clash["id"]}), 409
        updates.extend(["title = ?", "title_norm = ?"])
        values.extend([title, new_title_norm])

    if body is not None:
        if not isinstance(body, str):
            return jsonify({"error": "Некорректный текст"}), 400
        updates.append("body = ?")
        values.append(body)

    if not updates:
        note = get_knowledge_note(conn, note_id)
        return jsonify({"note": note, "graph": build_knowledge_graph(conn)})

    updates.append("updated_at = ?")
    values.append(iso(utc_now()))
    values.append(note_id)
    conn.execute(
        f"UPDATE knowledge_notes SET {', '.join(updates)} WHERE id = ?",
        values,
    )

    if body is not None:
        rebuild_note_links(conn, note_id, body)
    if title is not None:
        conn.execute(
            "UPDATE knowledge_links SET target_id = NULL WHERE target_id = ?",
            (note_id,),
        )
        resolve_incoming_links(conn, note_id, new_title_norm)
        # Refresh target_title display for resolved links pointing here
        display_title = title if title is not None else None
        if display_title is None:
            display_title = conn.execute(
                "SELECT title FROM knowledge_notes WHERE id = ?", (note_id,)
            ).fetchone()["title"]
        conn.execute(
            """
            UPDATE knowledge_links
            SET target_title = ?, target_title_norm = ?
            WHERE target_id = ?
            """,
            (display_title, new_title_norm, note_id),
        )

    conn.commit()
    note = get_knowledge_note(conn, note_id)
    return jsonify({"note": note, "graph": build_knowledge_graph(conn)})


@app.post("/api/knowledge/notes/<int:note_id>/delete")
def api_knowledge_delete_note(note_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT id, title_norm FROM knowledge_notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    if not row:
        abort(404)
    conn.execute(
        """
        UPDATE knowledge_links
        SET target_id = NULL
        WHERE target_id = ?
        """,
        (note_id,),
    )
    conn.execute("DELETE FROM knowledge_links WHERE source_id = ?", (note_id,))
    conn.execute("DELETE FROM knowledge_notes WHERE id = ?", (note_id,))
    conn.commit()
    return jsonify({"ok": True, "graph": build_knowledge_graph(conn)})


TRACKER_SCHEDULES = {"daily", "weekdays", "weekly", "interval", "once"}
TRACKER_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
TRACKER_TASK_TYPES = {"routine", "hourglass"}


def _tracker_date(value: str | None, default: date | None = None) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return default or datetime.now().astimezone().date()


def _tracker_form_data(form, existing: dict | None = None) -> dict:
    existing = existing or {}
    today = datetime.now().astimezone().date()
    schedule_type = (form.get("schedule_type") or existing.get("schedule_type") or "daily").strip()
    if schedule_type not in TRACKER_SCHEDULES:
        schedule_type = "daily"
    weekdays = []
    for raw in form.getlist("weekdays"):
        try:
            day = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in weekdays:
            weekdays.append(day)
    if not weekdays:
        stored = str(existing.get("weekdays") or "0,1,2,3,4,5,6")
        weekdays = [int(x) for x in stored.split(",") if x.isdigit() and 0 <= int(x) <= 6]
    if schedule_type == "weekdays" and not weekdays:
        weekdays = list(range(7))

    def positive_int(name: str, fallback: int, maximum: int = 999999) -> int:
        try:
            return max(1, min(maximum, int(form.get(name) or fallback)))
        except (TypeError, ValueError):
            return fallback

    start_date = _tracker_date(
        form.get("start_date"),
        _tracker_date(existing.get("start_date"), today),
    ).isoformat()
    due_date = None
    if schedule_type == "once":
        due_date = _tracker_date(
            form.get("due_date"),
            _tracker_date(existing.get("due_date"), _tracker_date(start_date)),
        ).isoformat()
        start_date = due_date
    color = (form.get("color") or existing.get("color") or "#5b8cff").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = "#5b8cff"

    task_type = (form.get("task_type") or existing.get("task_type") or "routine").strip()
    if task_type not in TRACKER_TASK_TYPES:
        task_type = "routine"

    if "budget_seconds" in form:
        try:
            budget_seconds = max(0, min(24 * 3600, int(form.get("budget_seconds") or 0)))
        except (TypeError, ValueError):
            budget_seconds = int(existing.get("budget_seconds") or 0)
    else:
        hours = form.get("budget_hours")
        minutes = form.get("budget_minutes")
        if hours is not None or minutes is not None:
            try:
                h = max(0, min(23, int(hours or 0)))
            except (TypeError, ValueError):
                h = 0
            try:
                m = max(0, min(59, int(minutes or 0)))
            except (TypeError, ValueError):
                m = 0
            budget_seconds = h * 3600 + m * 60
        else:
            budget_seconds = int(existing.get("budget_seconds") or 0)

    return {
        "title": re.sub(r"\s+", " ", (form.get("title") or "").strip())[:200],
        "note": (form.get("note") or "").strip()[:2000],
        "icon": (form.get("icon") or existing.get("icon") or "✓").strip()[:8] or "✓",
        "color": color,
        "task_type": task_type,
        "budget_seconds": budget_seconds,
        "schedule_type": schedule_type,
        "weekdays": ",".join(str(x) for x in sorted(weekdays)),
        "interval_days": positive_int("interval_days", int(existing.get("interval_days") or 1), 365),
        "weekly_target": positive_int("weekly_target", int(existing.get("weekly_target") or 1), 7),
        "target_value": positive_int("target_value", int(existing.get("target_value") or 1)),
        "unit": re.sub(r"\s+", " ", (form.get("unit") or existing.get("unit") or "раз").strip())[:32] or "раз",
        "start_date": start_date,
        "due_date": due_date,
    }


def _tracker_is_due(item: dict, day: date) -> bool:
    start = _tracker_date(item.get("start_date"))
    if day < start:
        return False
    schedule_type = item.get("schedule_type")
    if schedule_type == "daily":
        return True
    if schedule_type == "weekdays":
        days = {int(x) for x in str(item.get("weekdays") or "").split(",") if x.isdigit()}
        return day.weekday() in days
    if schedule_type == "weekly":
        return True
    if schedule_type == "interval":
        return (day - start).days % max(1, int(item.get("interval_days") or 1)) == 0
    if schedule_type == "once":
        return day == _tracker_date(item.get("due_date"), start)
    return False


def _tracker_schedule_label(item: dict) -> str:
    schedule_type = item.get("schedule_type")
    if schedule_type == "daily":
        return "Каждый день"
    if schedule_type == "weekdays":
        days = {int(x) for x in str(item.get("weekdays") or "").split(",") if x.isdigit()}
        if days == {0, 1, 2, 3, 4}:
            return "По будням"
        return " · ".join(TRACKER_WEEKDAYS[x] for x in range(7) if x in days) or "По дням недели"
    if schedule_type == "weekly":
        return f"{int(item.get('weekly_target') or 1)} р. в неделю"
    if schedule_type == "interval":
        days = int(item.get("interval_days") or 1)
        return f"Каждые {days} дн."
    if schedule_type == "once":
        return f"Разово · {_tracker_date(item.get('due_date')).strftime('%d.%m.%Y')}"
    return ""


def _tracker_entries(conn, item_id: int, start: date, end: date) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT entry_date, value FROM tracker_entries
        WHERE item_id = ? AND entry_date BETWEEN ? AND ?
        """,
        (item_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    return {r["entry_date"]: int(r["value"] or 0) for r in rows}


def _tracker_target(item: dict) -> int:
    if item.get("task_type") == "hourglass":
        return max(1, int(item.get("budget_seconds") or 0))
    return max(1, int(item.get("target_value") or 1))


def _tracker_accumulate(conn, item_id: int, entry_day: date, seconds: float) -> None:
    if seconds <= 0:
        return
    add = int(round(seconds))
    if add <= 0:
        return
    current = conn.execute(
        "SELECT value FROM tracker_entries WHERE item_id = ? AND entry_date = ?",
        (item_id, entry_day.isoformat()),
    ).fetchone()
    value = (int(current["value"] or 0) if current else 0) + add
    conn.execute(
        """
        INSERT INTO tracker_entries (item_id, entry_date, value, note, updated_at)
        VALUES (?, ?, ?, '', ?)
        ON CONFLICT(item_id, entry_date) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (item_id, entry_day.isoformat(), value, iso(utc_now())),
    )


def _tracker_stop_running(conn, item_id: int, running_since_iso: str, now: datetime) -> None:
    started = parse_iso(running_since_iso)
    if started is None:
        conn.execute("UPDATE tracker_items SET running_since = NULL WHERE id = ?", (item_id,))
        return
    local_started = started.astimezone()
    local_now = now.astimezone()
    cursor = local_started
    while cursor.date() < local_now.date():
        midnight = datetime.combine(
            cursor.date() + timedelta(days=1), datetime.min.time(), tzinfo=cursor.tzinfo
        )
        _tracker_accumulate(conn, item_id, cursor.date(), (midnight - cursor).total_seconds())
        cursor = midnight
    _tracker_accumulate(conn, item_id, cursor.date(), (local_now - cursor).total_seconds())
    conn.execute("UPDATE tracker_items SET running_since = NULL WHERE id = ?", (item_id,))


def _tracker_rollover_running(conn, now: datetime) -> None:
    local_now = now.astimezone()
    rows = conn.execute(
        "SELECT id, running_since FROM tracker_items WHERE task_type = 'hourglass' AND running_since IS NOT NULL"
    ).fetchall()
    for row in rows:
        started = parse_iso(row["running_since"])
        if started is None or started.astimezone().date() >= local_now.date():
            continue
        cursor = started.astimezone()
        while cursor.date() < local_now.date():
            midnight = datetime.combine(
                cursor.date() + timedelta(days=1), datetime.min.time(), tzinfo=cursor.tzinfo
            )
            _tracker_accumulate(conn, row["id"], cursor.date(), (midnight - cursor).total_seconds())
            cursor = midnight
        conn.execute(
            "UPDATE tracker_items SET running_since = ? WHERE id = ?", (iso(cursor), row["id"])
        )
    if rows:
        conn.commit()


def _tracker_streak(conn, item: dict, through: date) -> int:
    target = _tracker_target(item)
    if item.get("schedule_type") == "weekly":
        week = through - timedelta(days=through.weekday())
        rows = _tracker_entries(conn, item["id"], week - timedelta(days=371), week + timedelta(days=6))
        streak = 0
        for offset in range(54):
            ws = week - timedelta(days=offset * 7)
            hits = sum(
                1
                for n in range(7)
                if rows.get((ws + timedelta(days=n)).isoformat(), 0) >= target
            )
            if hits < int(item.get("weekly_target") or 1):
                if offset == 0:
                    continue
                break
            streak += 1
        return streak

    rows = _tracker_entries(conn, item["id"], through - timedelta(days=730), through)
    cursor = through
    streak = 0
    started = False
    for _ in range(731):
        if cursor < _tracker_date(item.get("start_date")):
            break
        if not _tracker_is_due(item, cursor):
            cursor -= timedelta(days=1)
            continue
        complete = rows.get(cursor.isoformat(), 0) >= target
        if not complete:
            if not started and cursor == through:
                cursor -= timedelta(days=1)
                continue
            break
        started = True
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _tracker_item_payload(conn, row, selected: date) -> dict:
    item = dict(row)
    target = _tracker_target(item)
    history_start = selected - timedelta(days=3)
    history_end = selected + timedelta(days=3)
    week_start = selected - timedelta(days=selected.weekday())
    range_start = min(history_start, week_start)
    range_end = max(history_end, week_start + timedelta(days=6))
    entries = _tracker_entries(conn, item["id"], range_start, range_end)
    value = entries.get(selected.isoformat(), 0)
    week_hits = sum(
        1
        for n in range(7)
        if entries.get((week_start + timedelta(days=n)).isoformat(), 0) >= target
    )
    if item["schedule_type"] == "weekly":
        progress_value = week_hits
        progress_target = int(item["weekly_target"] or 1)
    else:
        progress_value = value
        progress_target = target
    history = []
    for n in range(7):
        day = history_start + timedelta(days=n)
        day_value = entries.get(day.isoformat(), 0)
        history.append(
            {
                "date": day.isoformat(),
                "label": TRACKER_WEEKDAYS[day.weekday()],
                "day_num": day.day,
                "is_selected": day == selected,
                "due": _tracker_is_due(item, day),
                "complete": day_value >= target,
                "value": day_value,
            }
        )
    item.update(
        {
            "weekdays_list": [int(x) for x in item["weekdays"].split(",") if x.isdigit()],
            "schedule_label": _tracker_schedule_label(item),
            "due": _tracker_is_due(item, selected),
            "value": value,
            "complete": progress_value >= progress_target,
            "progress_value": progress_value,
            "progress_target": progress_target,
            "week_hits": week_hits,
            "streak": _tracker_streak(conn, item, selected),
            "history": history,
        }
    )
    if item.get("task_type") == "hourglass":
        item["remaining_seconds"] = max(0, int(item.get("budget_seconds") or 0) - value)
        item["is_running"] = bool(item.get("running_since"))
    return item


def _tracker_dashboard(conn, selected: date) -> dict:
    rows = conn.execute(
        """
        SELECT * FROM tracker_items
        WHERE archived = 0
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()
    items = [_tracker_item_payload(conn, row, selected) for row in rows]
    due_items = [item for item in items if item["due"]]
    done = sum(1 for item in due_items if item["complete"])
    return {
        "items": items,
        "due_items": due_items,
        "done": done,
        "due_count": len(due_items),
        "percent": round(done * 100 / len(due_items)) if due_items else 0,
        "best_streak": max((item["streak"] for item in items), default=0),
    }


@app.get("/tracker")
def tracker_home():
    conn = get_db()
    today = datetime.now().astimezone().date()
    _tracker_rollover_running(conn, utc_now())
    selected = _tracker_date(request.args.get("date"), today)
    dashboard = _tracker_dashboard(conn, selected)
    all_rows = conn.execute(
        "SELECT * FROM tracker_items ORDER BY archived ASC, sort_order ASC, id ASC"
    ).fetchall()
    all_items = [_tracker_item_payload(conn, row, selected) for row in all_rows]
    return render_template(
        "tracker.html",
        selected_date=selected,
        selected_iso=selected.isoformat(),
        selected_label=selected.strftime("%d.%m.%Y"),
        today_iso=today.isoformat(),
        prev_date=(selected - timedelta(days=1)).isoformat(),
        next_date=(selected + timedelta(days=1)).isoformat(),
        dashboard=dashboard,
        all_items=all_items,
        weekday_names=TRACKER_WEEKDAYS,
    )


@app.post("/tracker/items")
def tracker_create_item():
    data = _tracker_form_data(request.form)
    if not data["title"]:
        return redirect(url_for("tracker_home", date=request.form.get("view_date") or None))
    conn = get_db()
    order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tracker_items"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO tracker_items (
            title, note, icon, color, task_type, budget_seconds,
            schedule_type, weekdays, interval_days,
            weekly_target, target_value, unit, start_date, due_date,
            archived, sort_order, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            data["title"], data["note"], data["icon"], data["color"],
            data["task_type"], data["budget_seconds"],
            data["schedule_type"], data["weekdays"], data["interval_days"],
            data["weekly_target"], data["target_value"], data["unit"],
            data["start_date"], data["due_date"], order, iso(utc_now()),
        ),
    )
    conn.commit()
    return redirect(url_for("tracker_home", date=request.form.get("view_date") or None))


@app.post("/tracker/items/<int:item_id>/update")
def tracker_update_item(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    data = _tracker_form_data(request.form, dict(row))
    if not data["title"]:
        return redirect(url_for("tracker_home", date=request.form.get("view_date") or None))
    conn.execute(
        """
        UPDATE tracker_items SET
            title = ?, note = ?, icon = ?, color = ?, task_type = ?, budget_seconds = ?,
            schedule_type = ?, weekdays = ?, interval_days = ?, weekly_target = ?,
            target_value = ?, unit = ?, start_date = ?, due_date = ?
        WHERE id = ?
        """,
        (
            data["title"], data["note"], data["icon"], data["color"],
            data["task_type"], data["budget_seconds"],
            data["schedule_type"], data["weekdays"], data["interval_days"],
            data["weekly_target"], data["target_value"], data["unit"],
            data["start_date"], data["due_date"], item_id,
        ),
    )
    conn.commit()
    return redirect(url_for("tracker_home", date=request.form.get("view_date") or None))


@app.post("/tracker/items/<int:item_id>/archive")
def tracker_archive_item(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT archived, running_since FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    if row["running_since"]:
        _tracker_stop_running(conn, item_id, row["running_since"], utc_now())
    conn.execute(
        "UPDATE tracker_items SET archived = ? WHERE id = ?",
        (0 if row["archived"] else 1, item_id),
    )
    conn.commit()
    return redirect_back("tracker_home")


@app.post("/tracker/items/<int:item_id>/delete")
def tracker_delete_item(item_id: int):
    conn = get_db()
    conn.execute("DELETE FROM tracker_entries WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM tracker_items WHERE id = ?", (item_id,))
    conn.commit()
    return redirect(url_for("tracker_home"))


@app.post("/tracker/items/<int:item_id>/entry")
def tracker_set_entry(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Задача не найдена"}), 404
    body = request.get_json(silent=True) or request.form
    entry_date = _tracker_date(body.get("date"))
    current_row = conn.execute(
        "SELECT value FROM tracker_entries WHERE item_id = ? AND entry_date = ?",
        (item_id, entry_date.isoformat()),
    ).fetchone()
    current = int(current_row["value"] or 0) if current_row else 0
    if body.get("value") is not None:
        try:
            value = int(body.get("value"))
        except (TypeError, ValueError):
            return jsonify({"error": "Некорректное значение"}), 400
    else:
        try:
            value = current + int(body.get("delta") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Некорректное значение"}), 400
    value = max(0, min(999999, value))
    conn.execute(
        """
        INSERT INTO tracker_entries (item_id, entry_date, value, note, updated_at)
        VALUES (?, ?, ?, '', ?)
        ON CONFLICT(item_id, entry_date) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (item_id, entry_date.isoformat(), value, iso(utc_now())),
    )
    conn.commit()
    payload = _tracker_item_payload(conn, row, entry_date)
    dashboard = _tracker_dashboard(conn, entry_date)
    return jsonify(
        {
            "ok": True,
            "item": payload,
            "summary": {
                "done": dashboard["done"],
                "due_count": dashboard["due_count"],
                "percent": dashboard["percent"],
                "best_streak": dashboard["best_streak"],
            },
        }
    )


def _tracker_hourglass_response(conn, item_id: int, paused_ids: list[int]) -> dict:
    today = datetime.now().astimezone().date()
    row = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    payload = _tracker_item_payload(conn, row, today)
    dashboard = _tracker_dashboard(conn, today)
    paused_payloads = []
    for pid in paused_ids:
        prow = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (pid,)).fetchone()
        if prow:
            paused_payloads.append(_tracker_item_payload(conn, prow, today))
    return {
        "ok": True,
        "item": payload,
        "paused_items": paused_payloads,
        "summary": {
            "done": dashboard["done"],
            "due_count": dashboard["due_count"],
            "percent": dashboard["percent"],
            "best_streak": dashboard["best_streak"],
        },
    }


@app.post("/tracker/items/<int:item_id>/start")
def tracker_start_item(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Задача не найдена"}), 404
    if row["task_type"] != "hourglass":
        return jsonify({"error": "Эта задача не песочные часы"}), 400
    now = utc_now()
    others = conn.execute(
        """
        SELECT id, running_since FROM tracker_items
        WHERE task_type = 'hourglass' AND running_since IS NOT NULL AND id != ?
        """,
        (item_id,),
    ).fetchall()
    paused_ids = [o["id"] for o in others]
    for other in others:
        _tracker_stop_running(conn, other["id"], other["running_since"], now)
    if not row["running_since"]:
        conn.execute("UPDATE tracker_items SET running_since = ? WHERE id = ?", (iso(now), item_id))
    conn.commit()
    return jsonify(_tracker_hourglass_response(conn, item_id, paused_ids))


@app.post("/tracker/items/<int:item_id>/pause")
def tracker_pause_item(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Задача не найдена"}), 404
    if row["running_since"]:
        _tracker_stop_running(conn, item_id, row["running_since"], utc_now())
        conn.commit()
    return jsonify(_tracker_hourglass_response(conn, item_id, []))


@app.post("/tracker/items/<int:item_id>/reset-today")
def tracker_reset_today(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM tracker_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "Задача не найдена"}), 404
    if row["running_since"]:
        conn.execute("UPDATE tracker_items SET running_since = ? WHERE id = ?", (iso(utc_now()), item_id))
    today = datetime.now().astimezone().date()
    conn.execute(
        "DELETE FROM tracker_entries WHERE item_id = ? AND entry_date = ?",
        (item_id, today.isoformat()),
    )
    conn.commit()
    return jsonify(_tracker_hourglass_response(conn, item_id, []))


NW_PHOTOS_DIR = Path(__file__).parent / "static" / "nw_photos"
NW_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
NW_PHOTO_MAX = 2 * 1024 * 1024

NW_SETTING_DEFAULTS = {
    "cadence_list_1": "30",
    "cadence_list_2": "180",
    "cadence_list_3": "45",
    "birthday_lead_days": "14,3,0",
    "target_support": "5",
    "target_productivity": "75",
    "target_development": "100",
    "churn_target_pct": "30",
    "lock_enabled": "0",
    "pin_hash": "",
    "pin_salt": "",
    "mask_confidential": "1",
    "map_mode": "radial",
}

NW_CIRCLES = {
    "support": "Круг поддержки",
    "productivity": "Круг продуктивности",
    "development": "Круг развития",
}
NW_ROLES = {
    "connector": "Коннектор",
    "condenser": "Конденсатор",
    "gatekeeper": "Привратник",
    "bridge": "Мост",
    "conductor": "Проводник",
}
NW_LISTS = {
    "1": "Список 1 — ключевые",
    "2": "Список 2 — спящие",
    "3": "Список 3 — в развитии",
}
NW_STAGES = {
    "new": "Знакомство",
    "stabilizing": "Стабилизируются",
    "stable": "Стабильные",
    "trusted": "Доверительные",
}
NW_OPENNESS = {
    "": "—",
    "peach": "Персик",
    "apple": "Яблоко",
    "pomegranate": "Гранат",
}
NW_TALK = {
    "": "—",
    "great_communicator": "Великий коммуникатор",
    "attention_seeker": "Центр внимания",
    "controller": "Контролёр",
    "withdrawn": "Замкнутый",
    "mixed": "Смешанный",
}
NW_TRIAGE = {
    "": "—",
    "work": "Работать",
    "freeze": "Заморозить",
    "drop": "Отпустить",
}
NW_CHANNELS = {
    "meet": "Встреча",
    "call": "Звонок",
    "message": "Сообщение",
    "email": "Письмо",
    "social": "Соцсети",
    "gift": "Подарок",
    "other": "Другое",
}
NW_DATE_KINDS = {
    "birthday": "День рождения",
    "name_day": "Именины",
    "anniversary": "Годовщина",
    "work_event": "Рабочее событие",
    "holiday": "Праздник",
    "other": "Другое",
}
NW_OFFER_CATS = {
    "skill": "Навык",
    "contact": "Контакт",
    "intro": "Знакомство",
    "resource": "Ресурс",
    "hobby": "Хобби",
    "other": "Другое",
}
NW_PLACE_KINDS = {
    "club": "Клуб",
    "conference": "Конференция",
    "course": "Курс",
    "online": "Онлайн",
    "sport": "Спорт",
    "travel": "Путешествия",
    "charity": "Благотворительность",
    "other": "Другое",
}
NW_ADVISOR_ROLES = {
    "coach": "Тренер",
    "advisory": "Консультативный совет",
    "mentor": "Ментор",
    "role_model": "Образец",
    "rising_star": "Восходящая звезда",
}
NW_ORG_KINDS = {
    "vertical": "Вертикальная",
    "horizontal": "Горизонтальная",
    "mixed": "Смешанная",
    "virtual": "Виртуальная",
}
NW_DECISION = {
    "": "—",
    "king": "Царь",
    "committee": "Комитет",
    "bureaucratic": "Бюрократия",
}
NW_ORG_RINGS = {
    "core": "Ядро власти",
    "secondary": "Второй круг",
    "outer": "Периферия",
}
NW_EDGE_KIND = {
    "bureaucratic": "Бюрократические",
    "power": "Властные",
    "personal": "Личные связи",
}
NW_VERDICTS = {
    "talk": "Обсудить напрямую",
    "small_ask_test": "Тест маленькой просьбой",
    "reduce": "Снизить вложение",
    "release": "Отпустить",
    "keep": "Оставить как есть",
}
NW_SCORE_FACTORS = {
    "commitment": "Приверженность и интенсивность",
    "initiative": "Инициатива и взаимность",
    "emotional": "Эмоциональная вовлечённость",
    "openness": "Открытость и доверие",
}

NW_DOSSIER_GROUPS = [
    (
        "Персональное",
        [
            ("city", "Город"),
            ("origin", "Откуда родом"),
            ("languages", "Языки"),
            ("education", "Образование"),
            ("family", "Семья"),
            ("pets", "Питомцы"),
            ("health_notes", "Здоровье"),
            ("address", "Адрес"),
        ],
    ),
    (
        "Профессиональное",
        [
            ("company", "Компания"),
            ("position", "Должность"),
            ("industry", "Отрасль"),
            ("seniority", "Уровень"),
        ],
    ),
    (
        "Мотивация и обмен",
        [
            ("interests", "Интересы"),
            ("values_beliefs", "Ценности и убеждения"),
            ("motivators", "Что радует / что злит"),
            ("sensitive_topics", "Чего избегать"),
            ("can_give", "Что могу дать"),
            ("want_get", "Что хочу получить"),
            ("exchange_currency", "Чем обмениваемся"),
            ("vision", "Видение отношений"),
        ],
    ),
    (
        "История знакомства",
        [
            ("source", "Как познакомились"),
            ("first_contact_at", "Первый контакт"),
            ("common_places", "Где пересекаемся"),
        ],
    ),
    (
        "Контактные данные",
        [
            ("phones", "Телефоны"),
            ("emails", "Почта"),
            ("messengers", "Мессенджеры"),
            ("socials", "Соцсети"),
        ],
    ),
]

TALK_HINTS = {
    "great_communicator": "Сам рад заговорить: достаточно улыбки и вопроса, дальше направляйте разговор. Сложнее вежливо его завершить.",
    "attention_seeker": "Начните с искреннего комплимента выбору, стилю или работе — дальше человек охотно говорит о себе.",
    "controller": "Покажите, что цените порядок. Спросите о правилах и критериях «хорошо / плохо», дайте изложить свою картину.",
    "withdrawn": "Нужна явная причина для разговора. Двигайтесь медленно, дайте привыкнуть к присутствию, не тревожьте.",
    "mixed": "Смешайте подходы: начните с конкретной темы, следите за реакцией и подстраивайте темп.",
}
OPENNESS_HINTS = {
    "peach": "Быстро идёт на лёгкое знакомство, но до настоящего доверия далеко — нужен запас времени и терпения.",
    "pomegranate": "Сходится медленно и осторожно, зато потом открывается почти полностью.",
    "apple": "Середина: часть тем так и останется закрытой, сближение идёт умеренно.",
}
DECISION_HINTS = {
    "king": "Решает один человек. Стратегия — дойти до него и повлиять.",
    "committee": "Решает группа. Важно не нажить противников и оставаться нейтральным.",
    "bureaucratic": "Явного центра нет: нужно знать процесс и людей на каждом шаге.",
}
LIST_HINTS = {
    "1": "Длинная встреча раз в квартал, звонок раз в месяц, значимый подарок на день рождения.",
    "2": "Один-два звонка в год и поздравление с днём рождения.",
    "3": "Две-три предметные встречи, затем регулярные касания.",
}

NW_CONTACT_TEXT = [
    "display_name",
    "first_name",
    "last_name",
    "nickname",
    "photo_path",
    "circle",
    "priority_list",
    "stage",
    "roles",
    "openness_model",
    "talk_type",
    "triage_decision",
    "phones",
    "emails",
    "messengers",
    "socials",
    "address",
    "birth_date",
    "city",
    "origin",
    "languages",
    "education",
    "family",
    "pets",
    "health_notes",
    "company",
    "position",
    "industry",
    "seniority",
    "interests",
    "values_beliefs",
    "motivators",
    "sensitive_topics",
    "can_give",
    "want_get",
    "exchange_currency",
    "vision",
    "source",
    "first_contact_at",
    "common_places",
    "notes",
    "private_notes",
    "tags",
    "import_source",
]


def _migrate_networking(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nw_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS nw_sectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_interest_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6aa9ff',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            nickname TEXT NOT NULL DEFAULT '',
            photo_path TEXT NOT NULL DEFAULT '',
            circle TEXT NOT NULL DEFAULT 'development',
            priority_list TEXT NOT NULL DEFAULT '3',
            stage TEXT NOT NULL DEFAULT 'new',
            roles TEXT NOT NULL DEFAULT '',
            is_key INTEGER NOT NULL DEFAULT 0,
            importance INTEGER NOT NULL DEFAULT 2,
            leg_interests INTEGER NOT NULL DEFAULT 0,
            leg_empathy INTEGER NOT NULL DEFAULT 0,
            leg_circle INTEGER NOT NULL DEFAULT 0,
            openness_model TEXT NOT NULL DEFAULT '',
            talk_type TEXT NOT NULL DEFAULT '',
            triage_danger INTEGER NOT NULL DEFAULT 0,
            triage_interest INTEGER NOT NULL DEFAULT 0,
            triage_complexity INTEGER NOT NULL DEFAULT 0,
            triage_decision TEXT NOT NULL DEFAULT '',
            triage_at TEXT,
            phones TEXT NOT NULL DEFAULT '',
            emails TEXT NOT NULL DEFAULT '',
            messengers TEXT NOT NULL DEFAULT '',
            socials TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            birth_date TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            languages TEXT NOT NULL DEFAULT '',
            education TEXT NOT NULL DEFAULT '',
            family TEXT NOT NULL DEFAULT '',
            pets TEXT NOT NULL DEFAULT '',
            health_notes TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            position TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            seniority TEXT NOT NULL DEFAULT '',
            interests TEXT NOT NULL DEFAULT '',
            values_beliefs TEXT NOT NULL DEFAULT '',
            motivators TEXT NOT NULL DEFAULT '',
            sensitive_topics TEXT NOT NULL DEFAULT '',
            can_give TEXT NOT NULL DEFAULT '',
            want_get TEXT NOT NULL DEFAULT '',
            exchange_currency TEXT NOT NULL DEFAULT '',
            vision TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            introduced_by_id INTEGER REFERENCES nw_contacts(id) ON DELETE SET NULL,
            first_contact_at TEXT NOT NULL DEFAULT '',
            common_places TEXT NOT NULL DEFAULT '',
            cadence_days INTEGER,
            last_contact_at TEXT,
            next_contact_due TEXT,
            due_snooze_until TEXT,
            initiative_balance INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            private_notes TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            is_confidential INTEGER NOT NULL DEFAULT 0,
            import_source TEXT NOT NULL DEFAULT '',
            imported_at TEXT,
            archived_at TEXT,
            sector_id INTEGER REFERENCES nw_sectors(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nw_contacts_circle ON nw_contacts(circle);
        CREATE INDEX IF NOT EXISTS idx_nw_contacts_due ON nw_contacts(next_contact_due);
        CREATE INDEX IF NOT EXISTS idx_nw_contacts_archived ON nw_contacts(archived_at);
        CREATE TABLE IF NOT EXISTS nw_contact_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            is_confidential INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_nw_contact_fields_contact ON nw_contact_fields(contact_id);
        CREATE TABLE IF NOT EXISTS nw_contact_group_map (
            group_id INTEGER NOT NULL REFERENCES nw_interest_groups(id) ON DELETE CASCADE,
            contact_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            PRIMARY KEY (group_id, contact_id)
        );
        CREATE TABLE IF NOT EXISTS nw_contact_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            a_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            b_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            strength TEXT NOT NULL DEFAULT 'normal',
            quality TEXT NOT NULL DEFAULT 'neutral',
            direction TEXT NOT NULL DEFAULT 'mutual',
            label TEXT NOT NULL DEFAULT '',
            UNIQUE (a_id, b_id)
        );
        CREATE INDEX IF NOT EXISTS idx_nw_edges_a ON nw_contact_edges(a_id);
        CREATE INDEX IF NOT EXISTS idx_nw_edges_b ON nw_contact_edges(b_id);
        CREATE TABLE IF NOT EXISTS nw_map_positions (
            contact_id INTEGER PRIMARY KEY REFERENCES nw_contacts(id) ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'radial',
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER REFERENCES nw_contacts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'birthday',
            title TEXT NOT NULL DEFAULT '',
            month INTEGER NOT NULL,
            day INTEGER NOT NULL,
            year INTEGER,
            recurring INTEGER NOT NULL DEFAULT 1,
            lead_days TEXT NOT NULL DEFAULT '7,1,0',
            greeting_note TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nw_dates_md ON nw_dates(month, day);
        CREATE INDEX IF NOT EXISTS idx_nw_dates_contact ON nw_dates(contact_id);
        CREATE TABLE IF NOT EXISTS nw_date_greetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_id INTEGER NOT NULL REFERENCES nw_dates(id) ON DELETE CASCADE,
            occ_year INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            snooze_until TEXT,
            done_at TEXT,
            UNIQUE (date_id, occ_year)
        );
        CREATE TABLE IF NOT EXISTS nw_meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            planned_at TEXT,
            status TEXT NOT NULL DEFAULT 'planned',
            place TEXT NOT NULL DEFAULT '',
            prep_goal_relationship TEXT NOT NULL DEFAULT '',
            prep_goal_understanding TEXT NOT NULL DEFAULT '',
            prep_offer TEXT NOT NULL DEFAULT '',
            prep_ask TEXT NOT NULL DEFAULT '',
            prep_topics TEXT NOT NULL DEFAULT '',
            prep_hook TEXT NOT NULL DEFAULT '',
            retro_notes TEXT NOT NULL DEFAULT '',
            retro_next_steps TEXT NOT NULL DEFAULT '',
            retro_new_facts TEXT NOT NULL DEFAULT '',
            is_confidential INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nw_meetings_contact ON nw_meetings(contact_id);
        CREATE INDEX IF NOT EXISTS idx_nw_meetings_planned ON nw_meetings(planned_at);
        CREATE TABLE IF NOT EXISTS nw_interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            occurred_at TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'message',
            direction TEXT NOT NULL DEFAULT 'out',
            summary TEXT NOT NULL DEFAULT '',
            is_confidential INTEGER NOT NULL DEFAULT 0,
            meeting_id INTEGER REFERENCES nw_meetings(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nw_interactions_contact ON nw_interactions(contact_id, occurred_at);
        CREATE TABLE IF NOT EXISTS nw_rel_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            meeting_id INTEGER REFERENCES nw_meetings(id) ON DELETE SET NULL,
            taken_at TEXT NOT NULL,
            commitment INTEGER NOT NULL DEFAULT 0,
            initiative INTEGER NOT NULL DEFAULT 0,
            emotional INTEGER NOT NULL DEFAULT 0,
            openness INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_nw_rel_scores_contact ON nw_rel_scores(contact_id, taken_at);
        CREATE TABLE IF NOT EXISTS nw_goal_sectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_id INTEGER NOT NULL REFERENCES nw_goal_sectors(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            horizon TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 2,
            status TEXT NOT NULL DEFAULT 'active',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_goal_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL REFERENCES nw_goals(id) ON DELETE CASCADE,
            contact_id INTEGER REFERENCES nw_contacts(id) ON DELETE SET NULL,
            external_name TEXT NOT NULL DEFAULT '',
            where_to_find TEXT NOT NULL DEFAULT '',
            how_to_reach TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_orgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'mixed',
            decision_style TEXT NOT NULL DEFAULT '',
            my_goal TEXT NOT NULL DEFAULT '',
            bridges_notes TEXT NOT NULL DEFAULT '',
            condensers_notes TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_org_power_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES nw_orgs(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#e08a5b'
        );
        CREATE TABLE IF NOT EXISTS nw_org_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES nw_orgs(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_org_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES nw_orgs(id) ON DELETE CASCADE,
            contact_id INTEGER REFERENCES nw_contacts(id) ON DELETE SET NULL,
            external_name TEXT NOT NULL DEFAULT '',
            unit_id INTEGER REFERENCES nw_org_units(id) ON DELETE SET NULL,
            power_group_id INTEGER REFERENCES nw_org_power_groups(id) ON DELETE SET NULL,
            ring TEXT NOT NULL DEFAULT 'outer',
            role_title TEXT NOT NULL DEFAULT '',
            formal_power INTEGER NOT NULL DEFAULT 1,
            informal_power INTEGER NOT NULL DEFAULT 1,
            is_visionary INTEGER NOT NULL DEFAULT 0,
            is_rising_star INTEGER NOT NULL DEFAULT 0,
            influences_decision INTEGER NOT NULL DEFAULT 0,
            growth_potential INTEGER NOT NULL DEFAULT 0,
            open_to_me INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nw_org_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES nw_orgs(id) ON DELETE CASCADE,
            a_id INTEGER NOT NULL REFERENCES nw_org_members(id) ON DELETE CASCADE,
            b_id INTEGER NOT NULL REFERENCES nw_org_members(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'personal',
            quality TEXT NOT NULL DEFAULT 'neutral',
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS nw_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'other',
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_hooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            suitable_for TEXT NOT NULL DEFAULT '',
            used_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'other',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_place_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id INTEGER NOT NULL REFERENCES nw_places(id) ON DELETE CASCADE,
            visited_at TEXT NOT NULL,
            contacts_made INTEGER NOT NULL DEFAULT 0,
            useful_contacts INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS nw_advisors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER REFERENCES nw_contacts(id) ON DELETE SET NULL,
            external_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'advisory',
            focus TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_gatherings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            planned_at TEXT,
            place TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_gathering_guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gathering_id INTEGER NOT NULL REFERENCES nw_gatherings(id) ON DELETE CASCADE,
            contact_id INTEGER REFERENCES nw_contacts(id) ON DELETE SET NULL,
            external_name TEXT NOT NULL DEFAULT '',
            is_new_face INTEGER NOT NULL DEFAULT 0,
            is_star_guest INTEGER NOT NULL DEFAULT 0,
            rsvp TEXT NOT NULL DEFAULT 'invited'
        );
        CREATE TABLE IF NOT EXISTS nw_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            contact_id INTEGER REFERENCES nw_contacts(id) ON DELETE SET NULL,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT NOT NULL DEFAULT 'manual',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            done_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_nw_actions_due ON nw_actions(status, due_date);
        CREATE TABLE IF NOT EXISTS nw_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period TEXT NOT NULL,
            step1_fit TEXT NOT NULL DEFAULT '',
            step2_changes TEXT NOT NULL DEFAULT '',
            step3_plan TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nw_diagnoses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES nw_contacts(id) ON DELETE CASCADE,
            taken_at TEXT NOT NULL,
            flag_reschedules INTEGER NOT NULL DEFAULT 0,
            flag_no_initiative INTEGER NOT NULL DEFAULT 0,
            flag_no_openness INTEGER NOT NULL DEFAULT 0,
            flag_sudden_interest INTEGER NOT NULL DEFAULT 0,
            verdict TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    cols = _table_columns(conn, "nw_contacts")
    if "sector_id" not in cols:
        conn.execute(
            "ALTER TABLE nw_contacts ADD COLUMN sector_id INTEGER REFERENCES nw_sectors(id) ON DELETE SET NULL"
        )
    if "due_snooze_until" not in cols:
        conn.execute("ALTER TABLE nw_contacts ADD COLUMN due_snooze_until TEXT")
    conn.commit()


def local_today() -> date:
    return datetime.now().astimezone().date()


def nw_csv_list(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def nw_join_csv(values) -> str:
    return ",".join(v.strip() for v in values if v and str(v).strip())


def nw_int(val, default=0, lo=None, hi=None) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def nw_opt_int(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def nw_flag(val) -> int:
    if val in (True, 1, "1", "on", "true", "True", "yes"):
        return 1
    return 0


def nw_parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        if "T" in s or s.endswith("Z"):
            return parse_iso(s).astimezone().date()
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def nw_wants_json() -> bool:
    if request.is_json:
        return True
    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return True
    return (request.headers.get("X-Requested-With") or "") == "XMLHttpRequest"


def nw_payload() -> dict:
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return data if isinstance(data, dict) else {}
    data = request.form.to_dict(flat=True)
    return data


def nw_get(name, default=""):
    data = nw_payload()
    if name in data:
        val = data.get(name)
        return default if val is None else val
    if name in request.args:
        return request.args.get(name, default)
    return default


def nw_getlist(name) -> list[str]:
    if request.is_json:
        data = request.get_json(silent=True) or {}
        val = data.get(name, [])
        if isinstance(val, list):
            return [str(v) for v in val]
        if val in (None, ""):
            return []
        return [str(val)]
    return request.form.getlist(name)


def nw_ok(payload=None, redirect_to=None, code=200):
    if nw_wants_json():
        return jsonify(payload if payload is not None else {"ok": True}), code
    if redirect_to:
        return redirect(redirect_to)
    return redirect_back("networking_home")


def nw_err(message: str, code=400, redirect_to=None):
    if nw_wants_json():
        return jsonify({"error": message}), code
    if redirect_to:
        return redirect(redirect_to)
    return redirect_back("networking_home")


def nw_get_settings(conn) -> dict:
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM nw_settings")}
    out = dict(NW_SETTING_DEFAULTS)
    out.update(rows)
    return out


def nw_set_setting(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO nw_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value if value is not None else ""),
    )


def nw_hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), 200_000).hex()


def nw_lock_enabled(conn=None) -> bool:
    conn = conn or get_db()
    return nw_get_settings(conn).get("lock_enabled") == "1"


def nw_mask_on(conn=None) -> bool:
    conn = conn or get_db()
    return nw_get_settings(conn).get("mask_confidential") == "1"


def require_networking_unlock():
    if not nw_lock_enabled():
        return None
    if session.get("nw_unlocked"):
        return None
    if nw_wants_json():
        return jsonify({"error": "locked"}), 401
    nxt = request.full_path if request.query_string else request.path
    if nxt.endswith("?"):
        nxt = nxt[:-1]
    return redirect(url_for("networking_unlock", next=nxt))


def nw_effective_cadence(contact, settings) -> int:
    if contact["cadence_days"]:
        return max(1, int(contact["cadence_days"]))
    key = "cadence_list_" + str(contact["priority_list"] or "3")
    return max(1, nw_int(settings.get(key), 45, 1, 3650))


def nw_recompute_contact_due(conn, contact_id: int, settings=None):
    settings = settings or nw_get_settings(conn)
    row = conn.execute("SELECT * FROM nw_contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return
    c = dict(row)
    base = nw_parse_date(c.get("last_contact_at")) or nw_parse_date(c.get("first_contact_at")) or nw_parse_date(
        c.get("created_at")
    )
    if not base:
        base = local_today()
    due = base + timedelta(days=nw_effective_cadence(c, settings))
    conn.execute("UPDATE nw_contacts SET next_contact_due = ? WHERE id = ?", (due.isoformat(), contact_id))


def nw_recompute_all_dues(conn):
    settings = nw_get_settings(conn)
    ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM nw_contacts WHERE archived_at IS NULL")
    ]
    for cid in ids:
        nw_recompute_contact_due(conn, cid, settings)


def nw_parse_birth(birth: str) -> tuple[int | None, int | None, int | None]:
    birth = (birth or "").strip()
    if not birth:
        return None, None, None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", birth)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.match(r"^(\d{2})-(\d{2})$", birth)
    if m:
        return None, int(m.group(1)), int(m.group(2))
    return None, None, None


def nw_sync_birthday(conn, contact_id: int, birth_date: str):
    settings = nw_get_settings(conn)
    year, month, day = nw_parse_birth(birth_date)
    existing = conn.execute(
        "SELECT id FROM nw_dates WHERE contact_id = ? AND kind = 'birthday'",
        (contact_id,),
    ).fetchone()
    if not month or not day:
        if existing:
            conn.execute("DELETE FROM nw_dates WHERE id = ?", (existing["id"],))
        return
    lead = settings.get("birthday_lead_days") or "14,3,0"
    if existing:
        conn.execute(
            """
            UPDATE nw_dates SET month = ?, day = ?, year = ?, lead_days = ?, title = ?, active = 1
            WHERE id = ?
            """,
            (month, day, year, lead, "День рождения", existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO nw_dates (contact_id, kind, title, month, day, year, recurring, lead_days, greeting_note, active, created_at)
            VALUES (?, 'birthday', 'День рождения', ?, ?, ?, 1, ?, '', 1, ?)
            """,
            (contact_id, month, day, year, lead, iso(utc_now())),
        )


def nw_safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def nw_next_occurrence(month: int, day: int, year=None, recurring=1, today=None) -> date:
    today = today or local_today()
    if not recurring and year:
        return nw_safe_date(int(year), month, day)
    occ = nw_safe_date(today.year, month, day)
    if occ >= today:
        return occ
    return nw_safe_date(today.year + 1, month, day)


def nw_touch_contact(conn, contact_id: int, occurred_at: str):
    row = conn.execute("SELECT last_contact_at FROM nw_contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return
    prev = nw_parse_date(row["last_contact_at"])
    new = nw_parse_date(occurred_at)
    if new and (not prev or new >= prev):
        conn.execute(
            "UPDATE nw_contacts SET last_contact_at = ?, due_snooze_until = NULL, updated_at = ? WHERE id = ?",
            (occurred_at, iso(utc_now()), contact_id),
        )
    nw_recompute_contact_due(conn, contact_id)


def nw_add_interaction(conn, contact_id, occurred_at, channel, direction, summary, is_confidential=0, meeting_id=None):
    conn.execute(
        """
        INSERT INTO nw_interactions (contact_id, occurred_at, channel, direction, summary, is_confidential, meeting_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            occurred_at,
            channel or "message",
            direction or "out",
            summary or "",
            nw_flag(is_confidential),
            meeting_id,
            iso(utc_now()),
        ),
    )
    nw_touch_contact(conn, contact_id, occurred_at)


def nw_contact_name(row, mask=False) -> str:
    if mask and row.get("is_confidential"):
        return "Скрытый контакт"
    return row.get("display_name") or "Без имени"


def nw_roles_of(row) -> list[str]:
    return nw_csv_list(row.get("roles") if isinstance(row, dict) else row["roles"])


def nw_legs(row) -> int:
    return int(row["leg_interests"] or 0) + int(row["leg_empathy"] or 0) + int(row["leg_circle"] or 0)


def nw_active_contacts(conn, include_archived=False):
    sql = "SELECT * FROM nw_contacts"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    sql += " ORDER BY display_name COLLATE NOCASE"
    return [dict(r) for r in conn.execute(sql)]


def nw_contact_or_404(conn, contact_id: int):
    row = conn.execute("SELECT * FROM nw_contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        abort(404)
    return dict(row)


def nw_enrich_contact(conn, c: dict) -> dict:
    c = dict(c)
    c["roles_list"] = nw_roles_of(c)
    c["legs"] = nw_legs(c)
    c["circle_label"] = NW_CIRCLES.get(c.get("circle") or "", c.get("circle") or "")
    c["list_label"] = NW_LISTS.get(str(c.get("priority_list") or ""), "")
    c["stage_label"] = NW_STAGES.get(c.get("stage") or "", c.get("stage") or "")
    groups = conn.execute(
        """
        SELECT g.id, g.name, g.color
        FROM nw_interest_groups g
        JOIN nw_contact_group_map m ON m.group_id = g.id
        WHERE m.contact_id = ?
        ORDER BY g.sort_order, g.id
        """,
        (c["id"],),
    ).fetchall()
    c["groups"] = [dict(g) for g in groups]
    c["group_ids"] = [g["id"] for g in c["groups"]]
    return c


def nw_display_contacts(conn):
    mask = nw_mask_on(conn)
    return [nw_enrich_contact(conn, {**c, "display_name": nw_contact_name(c, mask)}) for c in nw_active_contacts(conn)]


def nw_set_contact_groups(conn, contact_id: int, group_ids: list[int]):
    conn.execute("DELETE FROM nw_contact_group_map WHERE contact_id = ?", (contact_id,))
    for gid in group_ids:
        if gid:
            conn.execute(
                "INSERT OR IGNORE INTO nw_contact_group_map (group_id, contact_id) VALUES (?, ?)",
                (gid, contact_id),
            )


def nw_apply_contact_payload(data: dict, existing=None) -> dict:
    existing = existing or {}
    out = {}
    for key in NW_CONTACT_TEXT:
        if key == "photo_path":
            out[key] = existing.get("photo_path") or ""
            continue
        if key in data:
            out[key] = (data.get(key) or "").strip() if isinstance(data.get(key), str) else (data.get(key) or "")
            if out[key] is None:
                out[key] = ""
            out[key] = str(out[key])
        else:
            out[key] = existing.get(key) or ""
    roles = data.get("roles")
    if isinstance(roles, list):
        out["roles"] = nw_join_csv(roles)
    elif "roles" in data:
        out["roles"] = (roles or "").strip()
    first = (out.get("first_name") or "").strip()
    last = (out.get("last_name") or "").strip()
    if not (out.get("display_name") or "").strip():
        out["display_name"] = (first + " " + last).strip()
    out["circle"] = out.get("circle") if out.get("circle") in NW_CIRCLES else (existing.get("circle") or "development")
    out["priority_list"] = out.get("priority_list") if str(out.get("priority_list")) in NW_LISTS else (
        existing.get("priority_list") or "3"
    )
    out["stage"] = out.get("stage") if out.get("stage") in NW_STAGES else (existing.get("stage") or "new")
    out["is_key"] = nw_flag(data["is_key"]) if "is_key" in data else nw_int(existing.get("is_key"), 0)
    out["importance"] = nw_int(data.get("importance", existing.get("importance", 2)), 2, 1, 5)
    out["leg_interests"] = nw_flag(data["leg_interests"]) if "leg_interests" in data else nw_int(existing.get("leg_interests"), 0)
    out["leg_empathy"] = nw_flag(data["leg_empathy"]) if "leg_empathy" in data else nw_int(existing.get("leg_empathy"), 0)
    out["leg_circle"] = nw_flag(data["leg_circle"]) if "leg_circle" in data else nw_int(existing.get("leg_circle"), 0)
    out["triage_danger"] = nw_int(data.get("triage_danger", existing.get("triage_danger", 0)), 0, 0, 3)
    out["triage_interest"] = nw_int(data.get("triage_interest", existing.get("triage_interest", 0)), 0, 0, 3)
    out["triage_complexity"] = nw_int(data.get("triage_complexity", existing.get("triage_complexity", 0)), 0, 0, 3)
    out["initiative_balance"] = nw_int(data.get("initiative_balance", existing.get("initiative_balance", 0)), 0, -3, 3)
    out["is_confidential"] = nw_flag(data["is_confidential"]) if "is_confidential" in data else nw_int(
        existing.get("is_confidential"), 0
    )
    out["sector_id"] = nw_opt_int(data["sector_id"]) if "sector_id" in data else existing.get("sector_id")
    out["introduced_by_id"] = nw_opt_int(data["introduced_by_id"]) if "introduced_by_id" in data else existing.get(
        "introduced_by_id"
    )
    if "cadence_days" in data:
        out["cadence_days"] = nw_opt_int(data.get("cadence_days"))
    else:
        out["cadence_days"] = existing.get("cadence_days")
    if "triage_at" in data:
        out["triage_at"] = (data.get("triage_at") or "").strip() or None
    else:
        out["triage_at"] = existing.get("triage_at")
    return out


def nw_insert_contact(conn, fields: dict) -> int:
    now = iso(utc_now())
    cols = [
        "display_name",
        "first_name",
        "last_name",
        "nickname",
        "photo_path",
        "circle",
        "priority_list",
        "stage",
        "roles",
        "is_key",
        "importance",
        "leg_interests",
        "leg_empathy",
        "leg_circle",
        "openness_model",
        "talk_type",
        "triage_danger",
        "triage_interest",
        "triage_complexity",
        "triage_decision",
        "triage_at",
        "phones",
        "emails",
        "messengers",
        "socials",
        "address",
        "birth_date",
        "city",
        "origin",
        "languages",
        "education",
        "family",
        "pets",
        "health_notes",
        "company",
        "position",
        "industry",
        "seniority",
        "interests",
        "values_beliefs",
        "motivators",
        "sensitive_topics",
        "can_give",
        "want_get",
        "exchange_currency",
        "vision",
        "source",
        "introduced_by_id",
        "first_contact_at",
        "common_places",
        "cadence_days",
        "initiative_balance",
        "notes",
        "private_notes",
        "tags",
        "is_confidential",
        "import_source",
        "imported_at",
        "sector_id",
        "created_at",
        "updated_at",
    ]
    fields = dict(fields)
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    fields.setdefault("imported_at", None)
    values = [fields.get(c) for c in cols]
    placeholders = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO nw_contacts ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )
    cid = cur.lastrowid
    nw_sync_birthday(conn, cid, fields.get("birth_date") or "")
    nw_recompute_contact_due(conn, cid)
    return cid


def nw_update_contact_row(conn, contact_id: int, fields: dict):
    fields = dict(fields)
    fields["updated_at"] = iso(utc_now())
    cols = [
        "display_name",
        "first_name",
        "last_name",
        "nickname",
        "circle",
        "priority_list",
        "stage",
        "roles",
        "is_key",
        "importance",
        "leg_interests",
        "leg_empathy",
        "leg_circle",
        "openness_model",
        "talk_type",
        "triage_danger",
        "triage_interest",
        "triage_complexity",
        "triage_decision",
        "triage_at",
        "phones",
        "emails",
        "messengers",
        "socials",
        "address",
        "birth_date",
        "city",
        "origin",
        "languages",
        "education",
        "family",
        "pets",
        "health_notes",
        "company",
        "position",
        "industry",
        "seniority",
        "interests",
        "values_beliefs",
        "motivators",
        "sensitive_topics",
        "can_give",
        "want_get",
        "exchange_currency",
        "vision",
        "source",
        "introduced_by_id",
        "first_contact_at",
        "common_places",
        "cadence_days",
        "initiative_balance",
        "notes",
        "private_notes",
        "tags",
        "is_confidential",
        "sector_id",
        "updated_at",
    ]
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE nw_contacts SET {sets} WHERE id = ?",
        [fields.get(c) for c in cols] + [contact_id],
    )
    nw_sync_birthday(conn, contact_id, fields.get("birth_date") or "")
    nw_recompute_contact_due(conn, contact_id)


def nw_delete_photo_file(photo_path: str):
    if not photo_path:
        return
    name = Path(photo_path).name
    path = NW_PHOTOS_DIR / name
    if path.exists() and path.parent == NW_PHOTOS_DIR:
        try:
            path.unlink()
        except OSError:
            pass


def nw_upcoming_dates(conn, horizon_days=60):
    today = local_today()
    rows = conn.execute(
        """
        SELECT d.*, c.display_name, c.is_confidential, c.id AS cid
        FROM nw_dates d
        LEFT JOIN nw_contacts c ON c.id = d.contact_id
        WHERE d.active = 1
        """
    ).fetchall()
    out = []
    mask = nw_mask_on(conn)
    for r in rows:
        d = dict(r)
        leads = [nw_int(x, 0) for x in nw_csv_list(d.get("lead_days") or "0")]
        max_lead = max(leads) if leads else 0
        cap = min(max(max_lead, 0), horizon_days)
        occ = nw_next_occurrence(d["month"], d["day"], d.get("year"), d.get("recurring", 1), today)
        days_until = (occ - today).days
        if days_until < 0 or days_until > cap:
            continue
        greet = conn.execute(
            "SELECT * FROM nw_date_greetings WHERE date_id = ? AND occ_year = ?",
            (d["id"], occ.year),
        ).fetchone()
        status = "pending"
        if greet:
            greet = dict(greet)
            if greet["status"] == "snoozed" and greet.get("snooze_until"):
                until = nw_parse_date(greet["snooze_until"])
                if until and until > today:
                    continue
            status = greet["status"]
        passed = [n for n in sorted(leads, reverse=True) if days_until <= n]
        name = "Общий праздник"
        if d.get("contact_id"):
            name = "Скрытый контакт" if (mask and d.get("is_confidential")) else (d.get("display_name") or "Контакт")
        out.append(
            {
                **d,
                "occ": occ.isoformat(),
                "occ_year": occ.year,
                "days_until": days_until,
                "status": status,
                "passed_leads": passed,
                "person": name,
            }
        )
    out.sort(key=lambda x: (x["days_until"], x.get("title") or ""))
    return out


def nw_due_groups(conn):
    today = local_today()
    rows = conn.execute(
        """
        SELECT * FROM nw_contacts
        WHERE archived_at IS NULL AND next_contact_due IS NOT NULL
        ORDER BY next_contact_due ASC
        """
    ).fetchall()
    overdue, today_list, week = [], [], []
    mask = nw_mask_on(conn)
    for r in rows:
        c = nw_enrich_contact(conn, dict(r))
        snooze = nw_parse_date(c.get("due_snooze_until"))
        if snooze and snooze > today:
            continue
        due = nw_parse_date(c.get("next_contact_due"))
        if not due:
            continue
        c["display_name"] = nw_contact_name(c, mask)
        c["days_delta"] = (today - due).days
        if due < today:
            overdue.append(c)
        elif due == today:
            today_list.append(c)
        elif due <= today + timedelta(days=7):
            week.append(c)
    return overdue, today_list, week


def nw_network_stats(conn):
    settings = nw_get_settings(conn)
    today = local_today()
    year_ago = (today - timedelta(days=365)).isoformat()
    day90 = iso(utc_now() - timedelta(days=90))
    counts = {}
    for key in NW_CIRCLES:
        counts[key] = conn.execute(
            "SELECT COUNT(*) FROM nw_contacts WHERE archived_at IS NULL AND circle = ?",
            (key,),
        ).fetchone()[0]
    total_active = conn.execute("SELECT COUNT(*) FROM nw_contacts WHERE archived_at IS NULL").fetchone()[0]
    acquired = conn.execute(
        "SELECT COUNT(*) FROM nw_contacts WHERE created_at >= ?",
        (year_ago,),
    ).fetchone()[0]
    lost = conn.execute(
        "SELECT COUNT(*) FROM nw_contacts WHERE archived_at IS NOT NULL AND archived_at >= ?",
        (year_ago,),
    ).fetchone()[0]
    churn = lost / max(1, total_active) * 100
    key_n = conn.execute(
        "SELECT COUNT(*) FROM nw_contacts WHERE archived_at IS NULL AND is_key = 1"
    ).fetchone()[0]
    touches = conn.execute(
        "SELECT COUNT(*) FROM nw_interactions WHERE occurred_at >= ?",
        (day90,),
    ).fetchone()[0]
    key_touches = conn.execute(
        """
        SELECT COUNT(*) FROM nw_interactions i
        JOIN nw_contacts c ON c.id = i.contact_id
        WHERE i.occurred_at >= ? AND c.is_key = 1
        """,
        (day90,),
    ).fetchone()[0]
    return {
        "counts": counts,
        "targets": {
            "support": nw_int(settings.get("target_support"), 5),
            "productivity": nw_int(settings.get("target_productivity"), 75),
            "development": nw_int(settings.get("target_development"), 100),
        },
        "total_active": total_active,
        "acquired": acquired,
        "lost": lost,
        "churn": round(churn, 1),
        "churn_target": nw_int(settings.get("churn_target_pct"), 30),
        "key_n": key_n,
        "key_share": round(key_n / max(1, total_active) * 100, 1),
        "key_touch_share": round(key_touches / max(1, touches) * 100, 1),
        "touches_90": touches,
    }


def nw_open_problem_ids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT d.contact_id FROM nw_diagnoses d
        JOIN (
            SELECT contact_id, MAX(id) AS mid FROM nw_diagnoses GROUP BY contact_id
        ) x ON x.mid = d.id
        WHERE d.verdict IN ('talk', 'small_ask_test', 'reduce', 'release')
           OR d.flag_reschedules + d.flag_no_initiative + d.flag_no_openness + d.flag_sudden_interest > 0
        """
    ).fetchall()
    return {r["contact_id"] for r in rows}


def nw_contact_md(conn, c: dict, include_conf: bool) -> str:
    lines = [f"## {c['display_name']}", ""]
    lines.append(f"- Круг: {NW_CIRCLES.get(c.get('circle'), c.get('circle'))}")
    lines.append(f"- Список: {NW_LISTS.get(str(c.get('priority_list')), '')}")
    lines.append(f"- Этап: {NW_STAGES.get(c.get('stage'), '')}")
    roles = [NW_ROLES.get(r, r) for r in nw_roles_of(c)]
    if roles:
        lines.append("- Роли: " + ", ".join(roles))
    if c.get("company") or c.get("position"):
        lines.append(f"- Работа: {c.get('position') or ''} {c.get('company') or ''}".strip())
    if c.get("last_contact_at"):
        lines.append(f"- Последнее касание: {c.get('last_contact_at')}")
    if c.get("next_contact_due"):
        lines.append(f"- Пора связаться: {c.get('next_contact_due')}")
    if include_conf or not c.get("is_confidential"):
        if c.get("notes"):
            lines += ["", "Заметки:", c["notes"]]
        if include_conf and c.get("private_notes"):
            lines += ["", "Личные заметки:", c["private_notes"]]
    dates = conn.execute(
        "SELECT * FROM nw_dates WHERE contact_id = ? AND active = 1 ORDER BY month, day",
        (c["id"],),
    ).fetchall()
    if dates:
        lines.append("")
        lines.append("Даты:")
        for d in dates:
            lines.append(f"- {d['title'] or NW_DATE_KINDS.get(d['kind'], d['kind'])}: {d['day']:02d}.{d['month']:02d}")
    return "\n".join(lines)


def nw_ui_context(conn):
    settings = nw_get_settings(conn)
    return {
        "nw_lock_enabled": settings.get("lock_enabled") == "1",
        "nw_unlocked": bool(session.get("nw_unlocked")),
        "nw_mask": settings.get("mask_confidential") == "1",
        "nw_circles": NW_CIRCLES,
        "nw_roles": NW_ROLES,
        "nw_lists": NW_LISTS,
        "nw_stages": NW_STAGES,
        "nw_openness": NW_OPENNESS,
        "nw_talk": NW_TALK,
        "nw_triage": NW_TRIAGE,
        "nw_channels": NW_CHANNELS,
        "nw_date_kinds": NW_DATE_KINDS,
        "nw_offer_cats": NW_OFFER_CATS,
        "nw_place_kinds": NW_PLACE_KINDS,
        "nw_advisor_roles": NW_ADVISOR_ROLES,
        "nw_org_kinds": NW_ORG_KINDS,
        "nw_decision": NW_DECISION,
        "nw_org_rings": NW_ORG_RINGS,
        "nw_edge_kind": NW_EDGE_KIND,
        "nw_verdicts": NW_VERDICTS,
        "nw_score_factors": NW_SCORE_FACTORS,
        "talk_hints": TALK_HINTS,
        "openness_hints": OPENNESS_HINTS,
        "decision_hints": DECISION_HINTS,
        "list_hints": LIST_HINTS,
        "dossier_groups": NW_DOSSIER_GROUPS,
    }


@app.context_processor
def inject_networking_ui():
    ep = request.endpoint or ""
    if not ep.startswith("networking"):
        return {}
    return nw_ui_context(get_db())


@app.before_request
def _networking_lock_gate():
    ep = request.endpoint or ""
    if ep.startswith("networking") and ep not in ("networking_unlock", "networking_unlock_post"):
        return require_networking_unlock()
    return None


def nw_norm_phone(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def nw_split_multi(s: str) -> list[str]:
    parts = re.split(r"[\n,;]+", s or "")
    return [p.strip() for p in parts if p.strip()]


def nw_tokens_overlap(a: str, b: str, phone=False) -> bool:
    la = nw_split_multi(a)
    lb = nw_split_multi(b)
    if phone:
        la = [nw_norm_phone(x) for x in la if nw_norm_phone(x)]
        lb = [nw_norm_phone(x) for x in lb if nw_norm_phone(x)]
    else:
        la = [x.lower() for x in la]
        lb = [x.lower() for x in lb]
    return bool(set(la) & set(lb))


def nw_find_duplicate(conn, display_name: str, phones: str, emails: str):
    name = (display_name or "").strip().lower()
    if not name:
        return None
    for r in conn.execute(
        "SELECT * FROM nw_contacts WHERE archived_at IS NULL AND lower(display_name) = ?",
        (name,),
    ):
        row = dict(r)
        if nw_tokens_overlap(phones, row.get("phones") or "", True) or nw_tokens_overlap(
            emails, row.get("emails") or "", False
        ):
            return row
    return None


def parse_vcard_text(text: str) -> list[dict]:
    cards = []
    current = {}
    pending = ""
    for raw in text.splitlines():
        if raw.startswith(" ") or raw.startswith("\t"):
            pending += raw[1:]
            continue
        if pending:
            line = pending
            pending = raw
        else:
            line = raw
            pending = ""
            if not current and not line:
                continue
        if pending:
            # process previous complete line stored in `line` after fold flush
            pass
        line = line.strip("\r")
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("BEGIN:VCARD"):
            current = {}
            continue
        if upper.startswith("END:VCARD"):
            if current:
                cards.append(current)
            current = {}
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        prop = key.split(";")[0].upper()
        val = val.replace("\\n", "\n").replace("\\,", ",")
        if prop == "FN":
            current["display_name"] = val.strip()
        elif prop == "N":
            parts = val.split(";")
            current["last_name"] = (parts[0] if parts else "").strip()
            current["first_name"] = (parts[1] if len(parts) > 1 else "").strip()
            if not current.get("display_name"):
                current["display_name"] = (current["first_name"] + " " + current["last_name"]).strip()
        elif prop == "TEL":
            current["phones"] = (current.get("phones") or "")
            current["phones"] = (current["phones"] + "\n" + val.strip()).strip()
        elif prop == "EMAIL":
            current["emails"] = (current.get("emails") or "")
            current["emails"] = (current["emails"] + "\n" + val.strip()).strip()
        elif prop == "ORG":
            current["company"] = val.split(";")[0].strip()
        elif prop == "TITLE":
            current["position"] = val.strip()
        elif prop == "BDAY":
            b = val.strip().replace("/", "-")
            if re.match(r"^\d{8}$", b):
                current["birth_date"] = f"{b[:4]}-{b[4:6]}-{b[6:]}"
            else:
                current["birth_date"] = b[:10]
        elif prop == "ADR":
            parts = [p for p in val.split(";") if p.strip()]
            current["address"] = ", ".join(parts)
        elif prop == "NOTE":
            current["notes"] = val.strip()
        elif prop == "URL":
            current["socials"] = (current.get("socials") or "")
            current["socials"] = (current["socials"] + "\n" + val.strip()).strip()
    if pending and current is not None:
        pass
    return cards


def parse_csv_contacts(text: str) -> tuple[list[str], list[dict]]:
    sample = text[:4096]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = reader.fieldnames or []
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() if v is not None else "" for k, v in row.items()})
    return headers, rows


CSV_FIELD_GUESS = {
    "name": "display_name",
    "displayname": "display_name",
    "display_name": "display_name",
    "фио": "display_name",
    "full_name": "display_name",
    "fullname": "display_name",
    "first": "first_name",
    "firstname": "first_name",
    "first_name": "first_name",
    "имя": "first_name",
    "last": "last_name",
    "lastname": "last_name",
    "last_name": "last_name",
    "фамилия": "last_name",
    "phone": "phones",
    "phones": "phones",
    "tel": "phones",
    "телефон": "phones",
    "email": "emails",
    "emails": "emails",
    "почта": "emails",
    "company": "company",
    "организация": "company",
    "компания": "company",
    "title": "position",
    "position": "position",
    "должность": "position",
    "city": "city",
    "город": "city",
    "note": "notes",
    "notes": "notes",
    "заметки": "notes",
    "bday": "birth_date",
    "birthday": "birth_date",
    "birth_date": "birth_date",
    "др": "birth_date",
    "tags": "tags",
    "теги": "tags",
}


# CSV-шаблон импорта: (заголовок в файле, поле контакта)
NW_IMPORT_TEMPLATE_COLUMNS = [
    ("ФИО", "display_name"),
    ("Имя", "first_name"),
    ("Фамилия", "last_name"),
    ("Телефон", "phones"),
    ("Почта", "emails"),
    ("Компания", "company"),
    ("Должность", "position"),
    ("Город", "city"),
    ("ДР", "birth_date"),
    ("Теги", "tags"),
    ("Заметки", "notes"),
]


def nw_import_template_headers() -> list[str]:
    return [h for h, _ in NW_IMPORT_TEMPLATE_COLUMNS]


def nw_contacts_template_csv(*, filled: bool) -> Response:
    """Пустой шаблон CSV или тот же шаблон с текущими контактами из БД."""
    headers = nw_import_template_headers()
    buf = io.StringIO()
    # BOM помогает Excel открыть UTF-8
    buf.write("\ufeff")
    w = csv.writer(buf, delimiter=";")
    w.writerow(headers)
    if filled:
        conn = get_db()
        mask = nw_mask_on(conn)
        rows = conn.execute(
            """
            SELECT display_name, first_name, last_name, phones, emails, company,
                   position, city, birth_date, tags, notes, is_confidential
            FROM nw_contacts
            WHERE archived_at IS NULL
            ORDER BY display_name COLLATE NOCASE
            """
        ).fetchall()
        for r in rows:
            c = dict(r)
            if c.get("is_confidential") and mask:
                w.writerow(["Скрытый контакт"] + [""] * (len(headers) - 1))
                continue
            vals = []
            for _header, field in NW_IMPORT_TEMPLATE_COLUMNS:
                vals.append((c.get(field) or "").replace("\r\n", "\n").replace("\r", "\n"))
            w.writerow(vals)
        filename = "contacts_template_filled.csv"
    else:
        filename = "contacts_template.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def guess_csv_mapping(headers: list[str]) -> dict:
    mapping = {}
    for h in headers:
        key = re.sub(r"\s+", "", (h or "").lower())
        mapping[h] = CSV_FIELD_GUESS.get(key, "")
    return mapping


def nw_select_contacts(conn, include_archived=False):
    sql = "SELECT id, display_name, is_confidential, archived_at FROM nw_contacts"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    sql += " ORDER BY display_name COLLATE NOCASE"
    mask = nw_mask_on(conn)
    out = []
    for r in conn.execute(sql):
        out.append({"id": r["id"], "display_name": nw_contact_name(dict(r), mask)})
    return out


def nw_ids_from_form(name="ids"):
    raw = nw_getlist(name)
    if not raw and nw_get(name):
        raw = [nw_get(name)]
    ids = []
    for x in raw:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    return ids


@app.get("/networking/unlock")
def networking_unlock():
    conn = get_db()
    if not nw_lock_enabled(conn):
        return redirect(url_for("networking_home"))
    if session.get("nw_unlocked"):
        return redirect(request.args.get("next") or url_for("networking_home"))
    return render_template("networking_unlock.html", error=None, next=request.args.get("next") or "")


@app.post("/networking/unlock")
def networking_unlock_post():
    conn = get_db()
    settings = nw_get_settings(conn)
    pin = (nw_get("pin") or "").strip()
    nxt = nw_get("next") or url_for("networking_home")
    if not nxt.startswith("/"):
        nxt = url_for("networking_home")
    salt = settings.get("pin_salt") or ""
    expected = settings.get("pin_hash") or ""
    if not salt or not expected:
        return render_template("networking_unlock.html", error="PIN не задан", next=nxt)
    got = nw_hash_pin(pin, salt)
    if not hmac.compare_digest(got, expected):
        return render_template("networking_unlock.html", error="Неверный код", next=nxt)
    session["nw_unlocked"] = True
    return redirect(nxt)


@app.post("/networking/lock")
def networking_lock():
    session.pop("nw_unlocked", None)
    return nw_ok({"ok": True}, url_for("networking_unlock"))


@app.get("/networking/")
def networking_home():
    conn = get_db()
    overdue, today_due, week_due = nw_due_groups(conn)
    dates = nw_upcoming_dates(conn)
    actions = [
        dict(r)
        for r in conn.execute(
            """
            SELECT a.*, c.display_name
            FROM nw_actions a
            LEFT JOIN nw_contacts c ON c.id = a.contact_id
            WHERE a.status = 'open'
            ORDER BY CASE WHEN a.due_date IS NULL OR a.due_date = '' THEN 1 ELSE 0 END, a.due_date ASC, a.id ASC
            """
        )
    ]
    today = local_today().isoformat()
    week_end = (local_today() + timedelta(days=7)).isoformat()
    for a in actions:
        a["overdue"] = bool(a.get("due_date") and a["due_date"] < today)
        a["soon"] = bool(a.get("due_date") and today <= a["due_date"] <= week_end)
    meetings = [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.*, c.display_name
            FROM nw_meetings m
            JOIN nw_contacts c ON c.id = m.contact_id
            WHERE m.status = 'planned'
            ORDER BY CASE WHEN m.planned_at IS NULL OR m.planned_at = '' THEN 1 ELSE 0 END, m.planned_at ASC
            LIMIT 20
            """
        )
    ]
    return render_template(
        "networking_home.html",
        overdue=overdue,
        today_due=today_due,
        week_due=week_due,
        dates=dates,
        actions=actions,
        meetings=meetings,
        stats=nw_network_stats(conn),
        contacts=nw_select_contacts(conn),
        sectors=[dict(r) for r in conn.execute("SELECT * FROM nw_sectors ORDER BY sort_order, id")],
    )


@app.get("/networking/contacts")
def networking_contacts():
    conn = get_db()
    q = (request.args.get("q") or "").strip()
    circle = request.args.get("circle") or ""
    plist = request.args.get("list") or ""
    role = request.args.get("role") or ""
    stage = request.args.get("stage") or ""
    group_id = request.args.get("group", type=int)
    tag = (request.args.get("tag") or "").strip()
    confidential = request.args.get("confidential") or ""
    archive = request.args.get("archive") or ""
    sort = request.args.get("sort") or "name"
    view = request.args.get("view") or "table"
    sql = "SELECT * FROM nw_contacts WHERE 1=1"
    args = []
    if archive == "1":
        sql += " AND archived_at IS NOT NULL"
    else:
        sql += " AND archived_at IS NULL"
    if q:
        like = f"%{q}%"
        sql += """ AND (
            display_name LIKE ? COLLATE NOCASE OR company LIKE ? COLLATE NOCASE
            OR tags LIKE ? COLLATE NOCASE OR notes LIKE ? COLLATE NOCASE
            OR nickname LIKE ? COLLATE NOCASE
        )"""
        args.extend([like, like, like, like, like])
    if circle:
        sql += " AND circle = ?"
        args.append(circle)
    if plist:
        sql += " AND priority_list = ?"
        args.append(plist)
    if stage:
        sql += " AND stage = ?"
        args.append(stage)
    if role:
        sql += " AND (',' || roles || ',') LIKE ?"
        args.append(f"%,{role},%")
    if confidential == "1":
        sql += " AND is_confidential = 1"
    if group_id:
        sql += " AND id IN (SELECT contact_id FROM nw_contact_group_map WHERE group_id = ?)"
        args.append(group_id)
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"
        args.append(f"%{tag}%")
    if sort == "due":
        sql += " ORDER BY next_contact_due IS NULL, next_contact_due ASC, display_name COLLATE NOCASE"
    elif sort == "touch":
        sql += " ORDER BY last_contact_at IS NULL, last_contact_at DESC, display_name COLLATE NOCASE"
    elif sort == "importance":
        sql += " ORDER BY importance DESC, is_key DESC, display_name COLLATE NOCASE"
    else:
        sql += " ORDER BY display_name COLLATE NOCASE"
    rows = [nw_enrich_contact(conn, dict(r)) for r in conn.execute(sql, args)]
    mask = nw_mask_on(conn)
    today = local_today()
    for c in rows:
        c["list_name"] = nw_contact_name(c, mask)
        due = nw_parse_date(c.get("next_contact_due"))
        c["overdue"] = bool(due and due < today)
    groups = [dict(r) for r in conn.execute("SELECT * FROM nw_interest_groups ORDER BY sort_order, id")]
    tags = set()
    for c in rows:
        for t in nw_csv_list(c.get("tags")):
            tags.add(t)
    return render_template(
        "networking_contacts.html",
        contacts=rows,
        groups=groups,
        tags=sorted(tags, key=str.lower),
        filters={
            "q": q,
            "circle": circle,
            "list": plist,
            "role": role,
            "stage": stage,
            "group": group_id or "",
            "tag": tag,
            "confidential": confidential,
            "archive": archive,
            "sort": sort,
            "view": view,
        },
        sectors=[dict(r) for r in conn.execute("SELECT * FROM nw_sectors ORDER BY sort_order, id")],
        today=today.isoformat(),
    )


@app.post("/networking/contacts")
def networking_contact_create():
    conn = get_db()
    data = nw_payload()
    fields = nw_apply_contact_payload(data)
    if not (fields.get("display_name") or "").strip():
        return nw_err("Укажите имя")
    cid = nw_insert_contact(conn, fields)
    gids = []
    for x in nw_getlist("group_ids"):
        n = nw_opt_int(x)
        if n:
            gids.append(n)
    nw_set_contact_groups(conn, cid, gids)
    conn.commit()
    if nw_wants_json():
        return jsonify({"ok": True, "id": cid, "url": url_for("networking_contact", contact_id=cid)})
    return redirect(url_for("networking_contact", contact_id=cid))


@app.get("/networking/contacts/<int:contact_id>")
def networking_contact(contact_id: int):
    conn = get_db()
    c = nw_enrich_contact(conn, nw_contact_or_404(conn, contact_id))
    fields = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_contact_fields WHERE contact_id = ? ORDER BY sort_order, id",
            (contact_id,),
        )
    ]
    interactions = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_interactions WHERE contact_id = ? ORDER BY occurred_at DESC, id DESC",
            (contact_id,),
        )
    ]
    meetings = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_meetings WHERE contact_id = ? ORDER BY planned_at DESC, id DESC",
            (contact_id,),
        )
    ]
    scores = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_rel_scores WHERE contact_id = ? ORDER BY taken_at ASC, id ASC",
            (contact_id,),
        )
    ]
    dates = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_dates WHERE contact_id = ? ORDER BY month, day, id",
            (contact_id,),
        )
    ]
    diagnoses = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_diagnoses WHERE contact_id = ? ORDER BY taken_at DESC, id DESC",
            (contact_id,),
        )
    ]
    others = [x for x in nw_select_contacts(conn) if x["id"] != contact_id]
    return render_template(
        "networking_contact.html",
        c=c,
        custom_fields=fields,
        interactions=interactions,
        meetings=meetings,
        scores=scores,
        dates=dates,
        diagnoses=diagnoses,
        others=others,
        groups=[dict(r) for r in conn.execute("SELECT * FROM nw_interest_groups ORDER BY sort_order, id")],
        sectors=[dict(r) for r in conn.execute("SELECT * FROM nw_sectors ORDER BY sort_order, id")],
        hooks=[dict(r) for r in conn.execute("SELECT * FROM nw_hooks WHERE active = 1 ORDER BY id DESC")],
        offers=[dict(r) for r in conn.execute("SELECT * FROM nw_offers WHERE active = 1 ORDER BY sort_order, id")],
    )


@app.post("/networking/contacts/<int:contact_id>")
def networking_contact_update(contact_id: int):
    conn = get_db()
    existing = nw_contact_or_404(conn, contact_id)
    fields = nw_apply_contact_payload(nw_payload(), existing)
    if not request.is_json:
        fields["roles"] = nw_join_csv(nw_getlist("roles"))
        for flag in ("is_key", "leg_interests", "leg_empathy", "leg_circle", "is_confidential"):
            fields[flag] = nw_flag(nw_get(flag))
        gids = []
        for x in nw_getlist("group_ids"):
            n = nw_opt_int(x)
            if n:
                gids.append(n)
        nw_set_contact_groups(conn, contact_id, gids)
    elif "group_ids" in nw_payload() or nw_getlist("group_ids"):
        gids = []
        for x in nw_getlist("group_ids"):
            n = nw_opt_int(x)
            if n:
                gids.append(n)
        nw_set_contact_groups(conn, contact_id, gids)
    if not (fields.get("display_name") or "").strip():
        return nw_err("Укажите имя")
    nw_update_contact_row(conn, contact_id, fields)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/archive")
def networking_contact_archive(contact_id: int):
    conn = get_db()
    c = nw_contact_or_404(conn, contact_id)
    if c.get("archived_at"):
        conn.execute(
            "UPDATE nw_contacts SET archived_at = NULL, updated_at = ? WHERE id = ?",
            (iso(utc_now()), contact_id),
        )
    else:
        conn.execute(
            "UPDATE nw_contacts SET archived_at = ?, updated_at = ? WHERE id = ?",
            (iso(utc_now()), iso(utc_now()), contact_id),
        )
    conn.commit()
    return nw_ok({"ok": True, "archived": not c.get("archived_at")}, url_for("networking_contacts"))


@app.post("/networking/contacts/<int:contact_id>/delete")
def networking_contact_delete(contact_id: int):
    conn = get_db()
    c = nw_contact_or_404(conn, contact_id)
    nw_delete_photo_file(c.get("photo_path") or "")
    conn.execute("DELETE FROM nw_contacts WHERE id = ?", (contact_id,))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contacts"))


@app.post("/networking/contacts/<int:contact_id>/photo")
def networking_contact_photo(contact_id: int):
    conn = get_db()
    c = nw_contact_or_404(conn, contact_id)
    f = request.files.get("photo")
    if not f or not f.filename:
        return nw_err("Выберите файл")
    ext = Path(f.filename).suffix.lower()
    if ext not in NW_PHOTO_EXTS:
        return nw_err("Допустимы jpg, png, webp")
    data = f.read()
    if len(data) > NW_PHOTO_MAX:
        return nw_err("Файл больше 2 МБ")
    NW_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    nw_delete_photo_file(c.get("photo_path") or "")
    name = f"{contact_id}-{uuid4().hex}{ext}"
    (NW_PHOTOS_DIR / name).write_bytes(data)
    rel = f"nw_photos/{name}"
    conn.execute(
        "UPDATE nw_contacts SET photo_path = ?, updated_at = ? WHERE id = ?",
        (rel, iso(utc_now()), contact_id),
    )
    conn.commit()
    return nw_ok({"ok": True, "photo_path": rel}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/fields")
def networking_contact_field_add(contact_id: int):
    conn = get_db()
    nw_contact_or_404(conn, contact_id)
    label = (nw_get("label") or "").strip()
    if not label:
        return nw_err("Укажите название поля")
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM nw_contact_fields WHERE contact_id = ?",
        (contact_id,),
    ).fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO nw_contact_fields (contact_id, label, value, is_confidential, sort_order)
        VALUES (?, ?, ?, ?, ?)
        """,
        (contact_id, label, nw_get("value") or "", nw_flag(nw_get("is_confidential")), max_order + 1),
    )
    conn.commit()
    return nw_ok({"ok": True, "id": cur.lastrowid}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/fields/<int:fid>")
def networking_contact_field_update(contact_id: int, fid: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM nw_contact_fields WHERE id = ? AND contact_id = ?",
        (fid, contact_id),
    ).fetchone()
    if not row:
        abort(404)
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_contact_fields WHERE id = ?", (fid,))
    else:
        label = (nw_get("label") or row["label"]).strip()
        conn.execute(
            """
            UPDATE nw_contact_fields SET label = ?, value = ?, is_confidential = ?, sort_order = ?
            WHERE id = ?
            """,
            (
                label,
                nw_get("value") if "value" in nw_payload() else row["value"],
                nw_flag(nw_get("is_confidential")) if "is_confidential" in nw_payload() else row["is_confidential"],
                nw_int(nw_get("sort_order"), row["sort_order"]),
                fid,
            ),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/interactions")
def networking_interaction_add(contact_id: int):
    conn = get_db()
    nw_contact_or_404(conn, contact_id)
    occurred = (nw_get("occurred_at") or "").strip() or iso(utc_now())
    nw_add_interaction(
        conn,
        contact_id,
        occurred,
        nw_get("channel") or "message",
        nw_get("direction") or "out",
        nw_get("summary") or "",
        nw_get("is_confidential"),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/triage")
def networking_contact_triage(contact_id: int):
    conn = get_db()
    nw_contact_or_404(conn, contact_id)
    decision = nw_get("triage_decision") or ""
    if decision not in ("", "work", "freeze", "drop"):
        return nw_err("Неверное решение")
    conn.execute(
        """
        UPDATE nw_contacts SET triage_danger = ?, triage_interest = ?, triage_complexity = ?,
            triage_decision = ?, triage_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            nw_int(nw_get("triage_danger"), 0, 0, 3),
            nw_int(nw_get("triage_interest"), 0, 0, 3),
            nw_int(nw_get("triage_complexity"), 0, 0, 3),
            decision,
            (nw_get("triage_at") or "").strip() or local_today().isoformat(),
            iso(utc_now()),
            contact_id,
        ),
    )
    if decision == "freeze":
        conn.execute(
            "UPDATE nw_contacts SET circle = 'development', stage = 'new' WHERE id = ?",
            (contact_id,),
        )
        c = nw_contact_or_404(conn, contact_id)
        conn.execute(
            """
            INSERT INTO nw_actions (title, contact_id, due_date, status, source, note, created_at)
            VALUES (?, ?, ?, 'open', 'triage', '', ?)
            """,
            (
                f"Вернуться к {c['display_name']}",
                contact_id,
                (local_today() + timedelta(days=30)).isoformat(),
                iso(utc_now()),
            ),
        )
    conn.commit()
    return nw_ok({"ok": True, "decision": decision}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/scores")
def networking_score_add(contact_id: int):
    conn = get_db()
    nw_contact_or_404(conn, contact_id)
    taken = (nw_get("taken_at") or "").strip() or iso(utc_now())
    conn.execute(
        """
        INSERT INTO nw_rel_scores (contact_id, meeting_id, taken_at, commitment, initiative, emotional, openness, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            nw_opt_int(nw_get("meeting_id")),
            taken,
            nw_int(nw_get("commitment"), 0, 0, 3),
            nw_int(nw_get("initiative"), 0, 0, 3),
            nw_int(nw_get("emotional"), 0, 0, 3),
            nw_int(nw_get("openness"), 0, 0, 3),
            nw_get("note") or "",
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/dates")
def networking_contact_date_add(contact_id: int):
    conn = get_db()
    nw_contact_or_404(conn, contact_id)
    month = nw_int(nw_get("month"), 0, 1, 12)
    day = nw_int(nw_get("day"), 0, 1, 31)
    if not month or not day:
        return nw_err("Укажите месяц и день")
    settings = nw_get_settings(conn)
    lead = (nw_get("lead_days") or "").strip() or settings.get("birthday_lead_days") or "7,1,0"
    conn.execute(
        """
        INSERT INTO nw_dates (contact_id, kind, title, month, day, year, recurring, lead_days, greeting_note, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            contact_id,
            nw_get("kind") or "other",
            nw_get("title") or "",
            month,
            day,
            nw_opt_int(nw_get("year")),
            0 if nw_get("recurring") in ("0", "false") else 1,
            lead,
            nw_get("greeting_note") or "",
            iso(utc_now()),
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contact", contact_id=contact_id))


@app.post("/networking/contacts/<int:contact_id>/snooze")
def networking_contact_snooze(contact_id: int):
    conn = get_db()
    nw_contact_or_404(conn, contact_id)
    days = nw_int(nw_get("days"), 7, 1, 365)
    until = (local_today() + timedelta(days=days)).isoformat()
    conn.execute(
        "UPDATE nw_contacts SET due_snooze_until = ?, updated_at = ? WHERE id = ?",
        (until, iso(utc_now()), contact_id),
    )
    conn.commit()
    return nw_ok({"ok": True, "until": until}, url_for("networking_home"))


@app.post("/networking/contacts/bulk")
def networking_contacts_bulk():
    conn = get_db()
    ids = nw_ids_from_form("ids")
    action = nw_get("action") or ""
    if not ids:
        return nw_err("Никого не выбрано")
    now = iso(utc_now())
    if action == "circle":
        circle = nw_get("circle")
        if circle not in NW_CIRCLES:
            return nw_err("Неверный круг")
        conn.executemany(
            "UPDATE nw_contacts SET circle = ?, updated_at = ? WHERE id = ?",
            [(circle, now, i) for i in ids],
        )
    elif action == "list":
        plist = str(nw_get("priority_list") or nw_get("list") or "")
        if plist not in NW_LISTS:
            return nw_err("Неверный список")
        conn.executemany(
            "UPDATE nw_contacts SET priority_list = ?, updated_at = ? WHERE id = ?",
            [(plist, now, i) for i in ids],
        )
        for i in ids:
            nw_recompute_contact_due(conn, i)
    elif action == "group":
        gid = nw_opt_int(nw_get("group_id"))
        if not gid:
            return nw_err("Выберите группу")
        for i in ids:
            conn.execute(
                "INSERT OR IGNORE INTO nw_contact_group_map (group_id, contact_id) VALUES (?, ?)",
                (gid, i),
            )
    elif action == "archive":
        conn.executemany(
            "UPDATE nw_contacts SET archived_at = ?, updated_at = ? WHERE id = ?",
            [(now, now, i) for i in ids],
        )
    else:
        return nw_err("Неизвестное действие")
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_contacts"))


@app.post("/networking/dates/<int:date_id>")
def networking_date_update(date_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_dates WHERE id = ?", (date_id,)).fetchone()
    if not row:
        abort(404)
    cid = row["contact_id"]
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_dates WHERE id = ?", (date_id,))
        conn.commit()
        dest = url_for("networking_contact", contact_id=cid) if cid else url_for("networking_settings")
        return nw_ok({"ok": True}, dest)
    conn.execute(
        """
        UPDATE nw_dates SET kind = ?, title = ?, month = ?, day = ?, year = ?, recurring = ?,
            lead_days = ?, greeting_note = ?, active = ?
        WHERE id = ?
        """,
        (
            nw_get("kind") or row["kind"],
            nw_get("title") if "title" in nw_payload() else row["title"],
            nw_int(nw_get("month"), row["month"], 1, 12),
            nw_int(nw_get("day"), row["day"], 1, 31),
            nw_opt_int(nw_get("year")) if "year" in nw_payload() else row["year"],
            nw_flag(nw_get("recurring")) if "recurring" in nw_payload() else row["recurring"],
            nw_get("lead_days") if "lead_days" in nw_payload() else row["lead_days"],
            nw_get("greeting_note") if "greeting_note" in nw_payload() else row["greeting_note"],
            nw_flag(nw_get("active")) if "active" in nw_payload() else row["active"],
            date_id,
        ),
    )
    conn.commit()
    dest = url_for("networking_contact", contact_id=cid) if cid else url_for("networking_home")
    return nw_ok({"ok": True}, dest)


@app.post("/networking/dates/<int:date_id>/greet")
def networking_date_greet(date_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_dates WHERE id = ?", (date_id,)).fetchone()
    if not row:
        abort(404)
    occ_year = nw_int(nw_get("occ_year"), local_today().year)
    status = nw_get("status") or "done"
    if status not in ("done", "snoozed", "skip"):
        return nw_err("Неверный статус")
    snooze_until = nw_get("snooze_until") or None
    if status == "snoozed" and not snooze_until:
        days = nw_int(nw_get("days"), 3, 1, 60)
        snooze_until = (local_today() + timedelta(days=days)).isoformat()
    done_at = iso(utc_now()) if status == "done" else None
    conn.execute(
        """
        INSERT INTO nw_date_greetings (date_id, occ_year, status, snooze_until, done_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date_id, occ_year) DO UPDATE SET status = excluded.status,
            snooze_until = excluded.snooze_until, done_at = excluded.done_at
        """,
        (date_id, occ_year, status, snooze_until, done_at),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_home"))


@app.get("/networking/map")
def networking_map():
    conn = get_db()
    settings = nw_get_settings(conn)
    return render_template(
        "networking_map.html",
        map_mode=settings.get("map_mode") or "radial",
        groups=[dict(r) for r in conn.execute("SELECT * FROM nw_interest_groups ORDER BY sort_order, id")],
        sectors=[dict(r) for r in conn.execute("SELECT * FROM nw_sectors ORDER BY sort_order, id")],
        contacts=nw_select_contacts(conn),
        stats=nw_network_stats(conn),
    )


@app.get("/networking/map/data")
def networking_map_data():
    conn = get_db()
    settings = nw_get_settings(conn)
    mask = nw_mask_on(conn)
    sectors = [
        dict(r)
        for r in conn.execute("SELECT id, name, sort_order AS 'order' FROM nw_sectors ORDER BY sort_order, id")
    ]
    groups = [dict(r) for r in conn.execute("SELECT id, name, color FROM nw_interest_groups ORDER BY sort_order, id")]
    problems = nw_open_problem_ids(conn)
    nodes = []
    shown_ids = []
    for r in conn.execute("SELECT * FROM nw_contacts WHERE archived_at IS NULL"):
        c = dict(r)
        if c.get("is_confidential") and mask:
            continue
        shown_ids.append(c["id"])
        g = conn.execute(
            """
            SELECT g.color FROM nw_interest_groups g
            JOIN nw_contact_group_map m ON m.group_id = g.id
            WHERE m.contact_id = ?
            ORDER BY g.sort_order, g.id LIMIT 1
            """,
            (c["id"],),
        ).fetchone()
        pos = conn.execute(
            "SELECT x, y, pinned FROM nw_map_positions WHERE contact_id = ?",
            (c["id"],),
        ).fetchone()
        nodes.append(
            {
                "id": c["id"],
                "name": c["display_name"],
                "circle": c["circle"],
                "sector_id": c.get("sector_id"),
                "importance": c.get("importance") or 2,
                "is_key": bool(c.get("is_key")),
                "roles": nw_roles_of(c),
                "stage": c.get("stage") or "new",
                "initiative_balance": c.get("initiative_balance") or 0,
                "group_color": g["color"] if g else None,
                "has_problem": c["id"] in problems,
                "last_contact_at": c.get("last_contact_at"),
                "next_contact_due": c.get("next_contact_due"),
                "legs": nw_legs(c),
                "pos": {"x": pos["x"], "y": pos["y"], "pinned": bool(pos["pinned"])} if pos else None,
            }
        )
    shown = set(shown_ids)
    edges = []
    for e in conn.execute("SELECT * FROM nw_contact_edges"):
        if e["a_id"] in shown and e["b_id"] in shown:
            edges.append(
                {
                    "id": e["id"],
                    "a": e["a_id"],
                    "b": e["b_id"],
                    "strength": e["strength"],
                    "quality": e["quality"],
                    "direction": e["direction"],
                    "label": e["label"],
                }
            )
    n = len(shown_ids)
    density = round(2 * len(edges) / (n * (n - 1)), 3) if n > 1 else 0.0
    return jsonify(
        {
            "me": {"label": "Я"},
            "sectors": sectors,
            "circles": {
                "support": {"target": nw_int(settings.get("target_support"), 5)},
                "productivity": {"target": nw_int(settings.get("target_productivity"), 75)},
                "development": {"target": nw_int(settings.get("target_development"), 100)},
            },
            "groups": groups,
            "nodes": nodes,
            "edges": edges,
            "density": density,
            "mode": settings.get("map_mode") or "radial",
        }
    )


@app.post("/networking/map/position")
def networking_map_position():
    conn = get_db()
    cid = nw_opt_int(nw_get("contact_id"))
    if not cid:
        return nw_err("Нет контакта")
    nw_contact_or_404(conn, cid)
    x = max(-1.0, min(1.0, float(nw_get("x") or 0)))
    y = max(-1.0, min(1.0, float(nw_get("y") or 0)))
    mode = nw_get("mode") or "radial"
    conn.execute(
        """
        INSERT INTO nw_map_positions (contact_id, mode, x, y, pinned)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(contact_id) DO UPDATE SET mode = excluded.mode, x = excluded.x, y = excluded.y, pinned = 1
        """,
        (cid, mode, x, y),
    )
    conn.commit()
    return jsonify({"ok": True})


@app.post("/networking/map/reset-positions")
def networking_map_reset():
    conn = get_db()
    conn.execute("DELETE FROM nw_map_positions")
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_map"))


@app.post("/networking/map/mode")
def networking_map_mode():
    conn = get_db()
    mode = nw_get("mode") or "radial"
    if mode not in ("radial", "free"):
        mode = "radial"
    nw_set_setting(conn, "map_mode", mode)
    conn.commit()
    return jsonify({"ok": True, "mode": mode})


@app.post("/networking/edges")
def networking_edge_save():
    conn = get_db()
    a = nw_opt_int(nw_get("a_id"))
    b = nw_opt_int(nw_get("b_id"))
    if not a or not b or a == b:
        return nw_err("Выберите двух разных людей")
    direction = nw_get("direction") or "mutual"
    if a > b:
        a, b = b, a
        if direction == "a_to_b":
            direction = "b_to_a"
        elif direction == "b_to_a":
            direction = "a_to_b"
    conn.execute(
        """
        INSERT INTO nw_contact_edges (a_id, b_id, strength, quality, direction, label)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(a_id, b_id) DO UPDATE SET strength = excluded.strength, quality = excluded.quality,
            direction = excluded.direction, label = excluded.label
        """,
        (
            a,
            b,
            nw_get("strength") or "normal",
            nw_get("quality") or "neutral",
            direction,
            nw_get("label") or "",
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_map"))


@app.post("/networking/edges/<int:edge_id>/delete")
def networking_edge_delete(edge_id: int):
    conn = get_db()
    conn.execute("DELETE FROM nw_contact_edges WHERE id = ?", (edge_id,))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_map"))


@app.get("/networking/meetings")
def networking_meetings():
    conn = get_db()
    status = request.args.get("status") or ""
    contact_id = request.args.get("contact_id", type=int)
    period = request.args.get("period") or ""
    sql = """
        SELECT m.*, c.display_name FROM nw_meetings m
        JOIN nw_contacts c ON c.id = m.contact_id
        WHERE 1=1
    """
    args = []
    if status:
        sql += " AND m.status = ?"
        args.append(status)
    if contact_id:
        sql += " AND m.contact_id = ?"
        args.append(contact_id)
    if period == "week":
        start = local_today().isoformat()
        end = (local_today() + timedelta(days=7)).isoformat()
        sql += " AND m.planned_at >= ? AND m.planned_at < ?"
        args.extend([start, end])
    elif period == "month":
        start = local_today().replace(day=1).isoformat()
        sql += " AND m.planned_at >= ?"
        args.append(start)
    sql += " ORDER BY CASE WHEN m.planned_at IS NULL OR m.planned_at = '' THEN 1 ELSE 0 END, m.planned_at DESC, m.id DESC"
    rows = [dict(r) for r in conn.execute(sql, args)]
    return render_template(
        "networking_meetings.html",
        meetings=rows,
        contacts=nw_select_contacts(conn),
        hooks=[dict(r) for r in conn.execute("SELECT * FROM nw_hooks WHERE active = 1 ORDER BY id DESC")],
        filters={"status": status, "contact_id": contact_id or "", "period": period},
    )


@app.post("/networking/meetings")
def networking_meeting_create():
    conn = get_db()
    cid = nw_opt_int(nw_get("contact_id"))
    if not cid:
        return nw_err("Выберите контакт")
    c = nw_contact_or_404(conn, cid)
    hook_id = nw_opt_int(nw_get("hook_id"))
    hook_text = nw_get("prep_hook") or ""
    if hook_id:
        h = conn.execute("SELECT * FROM nw_hooks WHERE id = ?", (hook_id,)).fetchone()
        if h:
            hook_text = h["title"] + ((" — " + h["detail"]) if h["detail"] else "")
            conn.execute("UPDATE nw_hooks SET used_count = used_count + 1 WHERE id = ?", (hook_id,))
    now = iso(utc_now())
    cur = conn.execute(
        """
        INSERT INTO nw_meetings (contact_id, title, planned_at, status, place, prep_hook, created_at, updated_at)
        VALUES (?, ?, ?, 'planned', ?, ?, ?, ?)
        """,
        (
            cid,
            (nw_get("title") or "").strip() or f"Встреча с {c['display_name']}",
            (nw_get("planned_at") or "").strip() or None,
            nw_get("place") or "",
            hook_text,
            now,
            now,
        ),
    )
    conn.commit()
    return nw_ok({"ok": True, "id": cur.lastrowid}, url_for("networking_meeting", meeting_id=cur.lastrowid))


@app.get("/networking/meetings/<int:meeting_id>")
def networking_meeting(meeting_id: int):
    conn = get_db()
    m = conn.execute(
        """
        SELECT m.*, c.display_name, c.leg_interests, c.leg_empathy, c.leg_circle, c.id AS cid
        FROM nw_meetings m JOIN nw_contacts c ON c.id = m.contact_id WHERE m.id = ?
        """,
        (meeting_id,),
    ).fetchone()
    if not m:
        abort(404)
    m = dict(m)
    scores = [
        dict(r)
        for r in conn.execute("SELECT * FROM nw_rel_scores WHERE meeting_id = ? ORDER BY taken_at", (meeting_id,))
    ]
    offers = [dict(r) for r in conn.execute("SELECT * FROM nw_offers WHERE active = 1 ORDER BY sort_order, id")]
    return render_template(
        "networking_meeting.html",
        m=m,
        scores=scores,
        offers=offers,
        hooks=[dict(r) for r in conn.execute("SELECT * FROM nw_hooks WHERE active = 1 ORDER BY id DESC")],
    )


@app.post("/networking/meetings/<int:meeting_id>")
def networking_meeting_update(meeting_id: int):
    conn = get_db()
    m = conn.execute("SELECT * FROM nw_meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not m:
        abort(404)
    data = nw_payload()
    fields = [
        "title",
        "planned_at",
        "place",
        "prep_goal_relationship",
        "prep_goal_understanding",
        "prep_offer",
        "prep_ask",
        "prep_topics",
        "prep_hook",
        "retro_notes",
        "retro_next_steps",
        "retro_new_facts",
    ]
    sets = []
    vals = []
    for f in fields:
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(data.get(f) or "")
    if "is_confidential" in data:
        sets.append("is_confidential = ?")
        vals.append(nw_flag(data.get("is_confidential")))
    sets.append("updated_at = ?")
    vals.append(iso(utc_now()))
    vals.append(meeting_id)
    conn.execute(f"UPDATE nw_meetings SET {', '.join(sets)} WHERE id = ?", vals)
    if nw_flag(nw_get("add_score")):
        conn.execute(
            """
            INSERT INTO nw_rel_scores (contact_id, meeting_id, taken_at, commitment, initiative, emotional, openness, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                m["contact_id"],
                meeting_id,
                iso(utc_now()),
                nw_int(nw_get("commitment"), 0, 0, 3),
                nw_int(nw_get("initiative"), 0, 0, 3),
                nw_int(nw_get("emotional"), 0, 0, 3),
                nw_int(nw_get("openness"), 0, 0, 3),
                nw_get("score_note") or "",
            ),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_meeting", meeting_id=meeting_id))


@app.post("/networking/meetings/<int:meeting_id>/done")
def networking_meeting_done(meeting_id: int):
    conn = get_db()
    m = conn.execute("SELECT * FROM nw_meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not m:
        abort(404)
    now = iso(utc_now())
    conn.execute("UPDATE nw_meetings SET status = 'done', updated_at = ? WHERE id = ?", (now, meeting_id))
    occurred = m["planned_at"] or now
    nw_add_interaction(
        conn,
        m["contact_id"],
        occurred,
        "meet",
        "mutual",
        m["title"] or "Встреча",
        m["is_confidential"],
        meeting_id,
    )
    if any(k in nw_payload() for k in ("commitment", "initiative", "emotional", "openness")):
        conn.execute(
            """
            INSERT INTO nw_rel_scores (contact_id, meeting_id, taken_at, commitment, initiative, emotional, openness, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                m["contact_id"],
                meeting_id,
                now,
                nw_int(nw_get("commitment"), 0, 0, 3),
                nw_int(nw_get("initiative"), 0, 0, 3),
                nw_int(nw_get("emotional"), 0, 0, 3),
                nw_int(nw_get("openness"), 0, 0, 3),
                nw_get("note") or "",
            ),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_meeting", meeting_id=meeting_id))


@app.post("/networking/meetings/<int:meeting_id>/cancel")
def networking_meeting_cancel(meeting_id: int):
    conn = get_db()
    conn.execute(
        "UPDATE nw_meetings SET status = 'canceled', updated_at = ? WHERE id = ?",
        (iso(utc_now()), meeting_id),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_meetings"))


@app.get("/networking/goals")
def networking_goals():
    conn = get_db()
    sectors = [dict(r) for r in conn.execute("SELECT * FROM nw_goal_sectors ORDER BY sort_order, id")]
    for s in sectors:
        s["goals"] = [
            dict(g)
            for g in conn.execute(
                "SELECT * FROM nw_goals WHERE sector_id = ? ORDER BY sort_order, id",
                (s["id"],),
            )
        ]
        for g in s["goals"]:
            g["links"] = [
                dict(l)
                for l in conn.execute(
                    """
                    SELECT l.*, c.display_name FROM nw_goal_links l
                    LEFT JOIN nw_contacts c ON c.id = l.contact_id
                    WHERE l.goal_id = ? ORDER BY l.sort_order, l.id
                    """,
                    (g["id"],),
                )
            ]
    return render_template("networking_goals.html", sectors=sectors, contacts=nw_select_contacts(conn))


@app.post("/networking/goal-sectors")
def networking_goal_sector_create():
    conn = get_db()
    name = (nw_get("name") or "").strip()
    if not name:
        return nw_err("Укажите название")
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM nw_goal_sectors").fetchone()[0]
    conn.execute("INSERT INTO nw_goal_sectors (name, sort_order) VALUES (?, ?)", (name, max_order + 1))
    conn.commit()
    dest = url_for("networking_settings") if nw_get("from") == "settings" else url_for("networking_goals")
    return nw_ok({"ok": True}, dest)


@app.post("/networking/goal-sectors/<int:sid>")
def networking_goal_sector_update(sid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_goal_sectors WHERE id = ?", (sid,))
    else:
        if "name" in nw_payload():
            conn.execute("UPDATE nw_goal_sectors SET name = ? WHERE id = ?", ((nw_get("name") or "").strip(), sid))
        row = conn.execute("SELECT * FROM nw_goal_sectors WHERE id = ?", (sid,)).fetchone()
        if row and nw_get("move") in ("up", "down"):
            if nw_get("move") == "up":
                other = conn.execute(
                    "SELECT * FROM nw_goal_sectors WHERE sort_order < ? ORDER BY sort_order DESC LIMIT 1",
                    (row["sort_order"],),
                ).fetchone()
            else:
                other = conn.execute(
                    "SELECT * FROM nw_goal_sectors WHERE sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                    (row["sort_order"],),
                ).fetchone()
            if other:
                conn.execute("UPDATE nw_goal_sectors SET sort_order = ? WHERE id = ?", (other["sort_order"], sid))
                conn.execute("UPDATE nw_goal_sectors SET sort_order = ? WHERE id = ?", (row["sort_order"], other["id"]))
    conn.commit()
    dest = url_for("networking_settings") if nw_get("from") == "settings" else url_for("networking_goals")
    return nw_ok({"ok": True}, dest)


@app.post("/networking/goals")
def networking_goal_create():
    conn = get_db()
    sid = nw_opt_int(nw_get("sector_id"))
    title = (nw_get("title") or "").strip()
    if not sid or not title:
        return nw_err("Укажите сектор и цель")
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM nw_goals WHERE sector_id = ?", (sid,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO nw_goals (sector_id, title, horizon, priority, status, sort_order, created_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        (sid, title, nw_get("horizon") or "", nw_int(nw_get("priority"), 2, 1, 3), max_order + 1, iso(utc_now())),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_goals"))


@app.post("/networking/goals/<int:gid>")
def networking_goal_update(gid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_goals WHERE id = ?", (gid,))
    else:
        data = nw_payload()
        sets, vals = [], []
        for f in ("title", "horizon", "status"):
            if f in data:
                sets.append(f"{f} = ?")
                vals.append(data.get(f) or "")
        if "priority" in data:
            sets.append("priority = ?")
            vals.append(nw_int(data.get("priority"), 2, 1, 3))
        if sets:
            vals.append(gid)
            conn.execute(f"UPDATE nw_goals SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_goals"))


@app.post("/networking/goal-links")
def networking_goal_link_create():
    conn = get_db()
    gid = nw_opt_int(nw_get("goal_id"))
    if not gid:
        return nw_err("Нет цели")
    conn.execute(
        """
        INSERT INTO nw_goal_links (goal_id, contact_id, external_name, where_to_find, how_to_reach, sort_order)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
            gid,
            nw_opt_int(nw_get("contact_id")),
            nw_get("external_name") or "",
            nw_get("where_to_find") or "",
            nw_get("how_to_reach") or "",
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_goals"))


@app.post("/networking/goal-links/<int:lid>")
def networking_goal_link_update(lid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_goal_links WHERE id = ?", (lid,)).fetchone()
    if not row:
        abort(404)
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_goal_links WHERE id = ?", (lid,))
    elif nw_flag(nw_get("create_contact")):
        name = (row["external_name"] or "").strip()
        if name:
            cid = nw_insert_contact(conn, nw_apply_contact_payload({"display_name": name}))
            conn.execute("UPDATE nw_goal_links SET contact_id = ?, external_name = '' WHERE id = ?", (cid, lid))
    else:
        conn.execute(
            """
            UPDATE nw_goal_links SET contact_id = ?, external_name = ?, where_to_find = ?, how_to_reach = ?
            WHERE id = ?
            """,
            (
                nw_opt_int(nw_get("contact_id")) if "contact_id" in nw_payload() else row["contact_id"],
                nw_get("external_name") if "external_name" in nw_payload() else row["external_name"],
                nw_get("where_to_find") if "where_to_find" in nw_payload() else row["where_to_find"],
                nw_get("how_to_reach") if "how_to_reach" in nw_payload() else row["how_to_reach"],
                lid,
            ),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_goals"))


@app.get("/networking/orgs")
def networking_orgs():
    conn = get_db()
    rows = []
    for r in conn.execute("SELECT * FROM nw_orgs ORDER BY name COLLATE NOCASE"):
        d = dict(r)
        d["member_count"] = conn.execute(
            "SELECT COUNT(*) FROM nw_org_members WHERE org_id = ?", (d["id"],)
        ).fetchone()[0]
        rows.append(d)
    return render_template("networking_orgs.html", orgs=rows)


@app.post("/networking/orgs")
def networking_org_create():
    conn = get_db()
    name = (nw_get("name") or "").strip()
    if not name:
        return nw_err("Укажите название")
    now = iso(utc_now())
    cur = conn.execute(
        """
        INSERT INTO nw_orgs (name, kind, decision_style, my_goal, bridges_notes, condensers_notes, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            nw_get("kind") or "mixed",
            nw_get("decision_style") or "",
            nw_get("my_goal") or "",
            nw_get("bridges_notes") or "",
            nw_get("condensers_notes") or "",
            nw_get("notes") or "",
            now,
            now,
        ),
    )
    conn.commit()
    return nw_ok({"ok": True, "id": cur.lastrowid}, url_for("networking_org", org_id=cur.lastrowid))


@app.get("/networking/orgs/<int:org_id>")
def networking_org(org_id: int):
    conn = get_db()
    org = conn.execute("SELECT * FROM nw_orgs WHERE id = ?", (org_id,)).fetchone()
    if not org:
        abort(404)
    org = dict(org)
    units = [
        dict(r)
        for r in conn.execute("SELECT * FROM nw_org_units WHERE org_id = ? ORDER BY sort_order, id", (org_id,))
    ]
    pgroups = [dict(r) for r in conn.execute("SELECT * FROM nw_org_power_groups WHERE org_id = ? ORDER BY id", (org_id,))]
    members = [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.*, c.display_name FROM nw_org_members m
            LEFT JOIN nw_contacts c ON c.id = m.contact_id
            WHERE m.org_id = ? ORDER BY m.id
            """,
            (org_id,),
        )
    ]
    for mem in members:
        mem["name"] = mem.get("display_name") or mem.get("external_name") or "Без имени"
        mem["is_candidate"] = bool(
            mem.get("influences_decision")
            and mem.get("power_group_id")
            and mem.get("growth_potential")
            and mem.get("open_to_me")
        )
    edges = [dict(r) for r in conn.execute("SELECT * FROM nw_org_edges WHERE org_id = ?", (org_id,))]
    return render_template(
        "networking_org.html",
        org=org,
        units=units,
        pgroups=pgroups,
        members=members,
        edges=edges,
        contacts=nw_select_contacts(conn),
    )


@app.post("/networking/orgs/<int:org_id>")
def networking_org_update(org_id: int):
    conn = get_db()
    if not conn.execute("SELECT id FROM nw_orgs WHERE id = ?", (org_id,)).fetchone():
        abort(404)
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_orgs WHERE id = ?", (org_id,))
        conn.commit()
        return nw_ok({"ok": True}, url_for("networking_orgs"))
    data = nw_payload()
    sets, vals = [], []
    for f in ("name", "kind", "decision_style", "my_goal", "bridges_notes", "condensers_notes", "notes"):
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(data.get(f) or "")
    sets.append("updated_at = ?")
    vals.append(iso(utc_now()))
    vals.append(org_id)
    conn.execute(f"UPDATE nw_orgs SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=org_id))


@app.get("/networking/orgs/<int:org_id>/data")
def networking_org_data(org_id: int):
    conn = get_db()
    org = conn.execute("SELECT * FROM nw_orgs WHERE id = ?", (org_id,)).fetchone()
    if not org:
        abort(404)
    units = [
        dict(r)
        for r in conn.execute("SELECT * FROM nw_org_units WHERE org_id = ? ORDER BY sort_order, id", (org_id,))
    ]
    pgroups = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM nw_org_power_groups WHERE org_id = ?", (org_id,))}
    members = []
    for r in conn.execute(
        """
        SELECT m.*, c.display_name FROM nw_org_members m
        LEFT JOIN nw_contacts c ON c.id = m.contact_id WHERE m.org_id = ?
        """,
        (org_id,),
    ):
        m = dict(r)
        pg = pgroups.get(m.get("power_group_id"))
        members.append(
            {
                "id": m["id"],
                "name": m.get("display_name") or m.get("external_name") or "Без имени",
                "ring": m.get("ring") or "outer",
                "unit_id": m.get("unit_id"),
                "power": max(m.get("formal_power") or 0, m.get("informal_power") or 0),
                "is_visionary": bool(m.get("is_visionary")),
                "is_rising_star": bool(m.get("is_rising_star")),
                "color": pg["color"] if pg else None,
                "is_candidate": bool(
                    m.get("influences_decision")
                    and m.get("power_group_id")
                    and m.get("growth_potential")
                    and m.get("open_to_me")
                ),
                "x": m.get("x") or 0,
                "y": m.get("y") or 0,
                "pinned": bool(m.get("pinned")),
                "contact_id": m.get("contact_id"),
            }
        )
    edges = [
        {"id": e["id"], "a": e["a_id"], "b": e["b_id"], "kind": e["kind"], "quality": e["quality"]}
        for e in conn.execute("SELECT * FROM nw_org_edges WHERE org_id = ?", (org_id,))
    ]
    return jsonify({"org": dict(org), "units": units, "members": members, "edges": edges})


@app.post("/networking/orgs/<int:org_id>/units")
def networking_org_unit_create(org_id: int):
    conn = get_db()
    name = (nw_get("name") or "").strip()
    if not name:
        return nw_err("Укажите название")
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM nw_org_units WHERE org_id = ?", (org_id,)
    ).fetchone()[0]
    conn.execute("INSERT INTO nw_org_units (org_id, name, sort_order) VALUES (?, ?, ?)", (org_id, name, max_order + 1))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=org_id))


@app.post("/networking/org-units/<int:uid>")
def networking_org_unit_update(uid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_org_units WHERE id = ?", (uid,)).fetchone()
    if not row:
        abort(404)
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_org_units WHERE id = ?", (uid,))
    else:
        conn.execute("UPDATE nw_org_units SET name = ? WHERE id = ?", ((nw_get("name") or row["name"]).strip(), uid))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=row["org_id"]))


@app.post("/networking/orgs/<int:org_id>/power-groups")
def networking_org_pg_create(org_id: int):
    conn = get_db()
    name = (nw_get("name") or "").strip()
    if not name:
        return nw_err("Укажите название")
    conn.execute(
        "INSERT INTO nw_org_power_groups (org_id, name, color) VALUES (?, ?, ?)",
        (org_id, name, nw_get("color") or "#e08a5b"),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=org_id))


@app.post("/networking/org-power-groups/<int:pid>")
def networking_org_pg_update(pid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_org_power_groups WHERE id = ?", (pid,)).fetchone()
    if not row:
        abort(404)
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_org_power_groups WHERE id = ?", (pid,))
    else:
        conn.execute(
            "UPDATE nw_org_power_groups SET name = ?, color = ? WHERE id = ?",
            ((nw_get("name") or row["name"]).strip(), nw_get("color") or row["color"], pid),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=row["org_id"]))


@app.post("/networking/orgs/<int:org_id>/members")
def networking_org_member_create(org_id: int):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO nw_org_members (
            org_id, contact_id, external_name, unit_id, power_group_id, ring, role_title,
            formal_power, informal_power, is_visionary, is_rising_star,
            influences_decision, growth_potential, open_to_me, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            org_id,
            nw_opt_int(nw_get("contact_id")),
            nw_get("external_name") or "",
            nw_opt_int(nw_get("unit_id")),
            nw_opt_int(nw_get("power_group_id")),
            nw_get("ring") or "outer",
            nw_get("role_title") or "",
            nw_int(nw_get("formal_power"), 1, 0, 3),
            nw_int(nw_get("informal_power"), 1, 0, 3),
            nw_flag(nw_get("is_visionary")),
            nw_flag(nw_get("is_rising_star")),
            nw_flag(nw_get("influences_decision")),
            nw_flag(nw_get("growth_potential")),
            nw_flag(nw_get("open_to_me")),
            nw_get("notes") or "",
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=org_id))


@app.post("/networking/org-members/<int:mid>")
def networking_org_member_update(mid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_org_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        abort(404)
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_org_members WHERE id = ?", (mid,))
        conn.commit()
        return nw_ok({"ok": True}, url_for("networking_org", org_id=row["org_id"]))
    if "x" in nw_payload() and "y" in nw_payload():
        conn.execute(
            "UPDATE nw_org_members SET x = ?, y = ?, pinned = 1 WHERE id = ?",
            (float(nw_get("x") or 0), float(nw_get("y") or 0), mid),
        )
        conn.commit()
        return jsonify({"ok": True})
    data = nw_payload()
    sets, vals = [], []
    for f in ("external_name", "ring", "role_title", "notes"):
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(data.get(f) or "")
    for f in ("contact_id", "unit_id", "power_group_id"):
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(nw_opt_int(data.get(f)))
    for f in ("formal_power", "informal_power"):
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(nw_int(data.get(f), 1, 0, 3))
    for f in ("is_visionary", "is_rising_star", "influences_decision", "growth_potential", "open_to_me"):
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(nw_flag(data.get(f)))
    if sets:
        vals.append(mid)
        conn.execute(f"UPDATE nw_org_members SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=row["org_id"]))


@app.post("/networking/orgs/<int:org_id>/edges")
def networking_org_edge_create(org_id: int):
    conn = get_db()
    a = nw_opt_int(nw_get("a_id"))
    b = nw_opt_int(nw_get("b_id"))
    if not a or not b or a == b:
        return nw_err("Выберите двух участников")
    conn.execute(
        "INSERT INTO nw_org_edges (org_id, a_id, b_id, kind, quality, note) VALUES (?, ?, ?, ?, ?, ?)",
        (org_id, a, b, nw_get("kind") or "personal", nw_get("quality") or "neutral", nw_get("note") or ""),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=org_id))


@app.post("/networking/org-edges/<int:eid>/delete")
def networking_org_edge_delete(eid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM nw_org_edges WHERE id = ?", (eid,)).fetchone()
    if not row:
        abort(404)
    conn.execute("DELETE FROM nw_org_edges WHERE id = ?", (eid,))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_org", org_id=row["org_id"]))


@app.get("/networking/catalogs")
def networking_catalogs():
    conn = get_db()
    offers = [dict(r) for r in conn.execute("SELECT * FROM nw_offers ORDER BY sort_order, id")]
    hooks = [dict(r) for r in conn.execute("SELECT * FROM nw_hooks ORDER BY id DESC")]
    places = []
    for r in conn.execute("SELECT * FROM nw_places ORDER BY title COLLATE NOCASE"):
        p = dict(r)
        vis = [
            dict(v)
            for v in conn.execute(
                "SELECT * FROM nw_place_visits WHERE place_id = ? ORDER BY visited_at DESC", (p["id"],)
            )
        ]
        p["visits"] = vis
        n = len(vis)
        useful = sum(v["useful_contacts"] or 0 for v in vis)
        p["efficiency"] = round(useful / n, 2) if n else 0
        p["visit_count"] = n
        places.append(p)
    places.sort(key=lambda x: (-x["efficiency"], x["title"].lower()))
    advisors = [
        dict(r)
        for r in conn.execute(
            """
            SELECT a.*, c.display_name FROM nw_advisors a
            LEFT JOIN nw_contacts c ON c.id = a.contact_id
            ORDER BY a.id DESC
            """
        )
    ]
    return render_template(
        "networking_catalogs.html",
        offers=offers,
        hooks=hooks,
        places=places,
        advisors=advisors,
        contacts=nw_select_contacts(conn),
        tab=request.args.get("tab") or "offers",
    )


@app.post("/networking/offers")
def networking_offer_create():
    conn = get_db()
    title = (nw_get("title") or "").strip()
    if not title:
        return nw_err("Укажите название")
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM nw_offers").fetchone()[0]
    conn.execute(
        "INSERT INTO nw_offers (category, title, detail, active, sort_order, created_at) VALUES (?, ?, ?, 1, ?, ?)",
        (nw_get("category") or "other", title, nw_get("detail") or "", max_order + 1, iso(utc_now())),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="offers"))


@app.post("/networking/offers/<int:oid>")
def networking_offer_update(oid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_offers WHERE id = ?", (oid,))
    else:
        data = nw_payload()
        sets, vals = [], []
        for f in ("category", "title", "detail"):
            if f in data:
                sets.append(f"{f} = ?")
                vals.append(data.get(f) or "")
        if "active" in data or not request.is_json:
            sets.append("active = ?")
            vals.append(nw_flag(data.get("active")))
        if sets:
            vals.append(oid)
            conn.execute(f"UPDATE nw_offers SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="offers"))


@app.post("/networking/hooks")
def networking_hook_create():
    conn = get_db()
    title = (nw_get("title") or "").strip()
    if not title:
        return nw_err("Укажите зацепку")
    conn.execute(
        "INSERT INTO nw_hooks (title, detail, suitable_for, used_count, active, created_at) VALUES (?, ?, ?, 0, 1, ?)",
        (title, nw_get("detail") or "", nw_get("suitable_for") or "", iso(utc_now())),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="hooks"))


@app.post("/networking/hooks/<int:hid>")
def networking_hook_update(hid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_hooks WHERE id = ?", (hid,))
    else:
        data = nw_payload()
        sets, vals = [], []
        for f in ("title", "detail", "suitable_for"):
            if f in data:
                sets.append(f"{f} = ?")
                vals.append(data.get(f) or "")
        if "active" in data or not request.is_json:
            sets.append("active = ?")
            vals.append(nw_flag(data.get("active")))
        if sets:
            vals.append(hid)
            conn.execute(f"UPDATE nw_hooks SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="hooks"))


@app.post("/networking/places")
def networking_place_create():
    conn = get_db()
    title = (nw_get("title") or "").strip()
    if not title:
        return nw_err("Укажите место")
    conn.execute(
        "INSERT INTO nw_places (title, kind, notes, active, created_at) VALUES (?, ?, ?, 1, ?)",
        (title, nw_get("kind") or "other", nw_get("notes") or "", iso(utc_now())),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="places"))


@app.post("/networking/places/<int:pid>")
def networking_place_update(pid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_places WHERE id = ?", (pid,))
    else:
        data = nw_payload()
        sets, vals = [], []
        for f in ("title", "kind", "notes"):
            if f in data:
                sets.append(f"{f} = ?")
                vals.append(data.get(f) or "")
        if "active" in data or not request.is_json:
            sets.append("active = ?")
            vals.append(nw_flag(data.get("active")))
        if sets:
            vals.append(pid)
            conn.execute(f"UPDATE nw_places SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="places"))


@app.post("/networking/places/<int:pid>/visits")
def networking_place_visit_add(pid: int):
    conn = get_db()
    if not conn.execute("SELECT id FROM nw_places WHERE id = ?", (pid,)).fetchone():
        abort(404)
    conn.execute(
        "INSERT INTO nw_place_visits (place_id, visited_at, contacts_made, useful_contacts, note) VALUES (?, ?, ?, ?, ?)",
        (
            pid,
            (nw_get("visited_at") or "").strip() or local_today().isoformat(),
            nw_int(nw_get("contacts_made"), 0, 0, 1000),
            nw_int(nw_get("useful_contacts"), 0, 0, 1000),
            nw_get("note") or "",
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="places"))


@app.post("/networking/place-visits/<int:vid>/delete")
def networking_place_visit_delete(vid: int):
    conn = get_db()
    conn.execute("DELETE FROM nw_place_visits WHERE id = ?", (vid,))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="places"))


@app.post("/networking/advisors")
def networking_advisor_create():
    conn = get_db()
    conn.execute(
        "INSERT INTO nw_advisors (contact_id, external_name, role, focus, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            nw_opt_int(nw_get("contact_id")),
            nw_get("external_name") or "",
            nw_get("role") or "advisory",
            nw_get("focus") or "",
            nw_get("note") or "",
            iso(utc_now()),
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="advisors"))


@app.post("/networking/advisors/<int:aid>")
def networking_advisor_update(aid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_advisors WHERE id = ?", (aid,))
    else:
        data = nw_payload()
        sets, vals = [], []
        if "contact_id" in data:
            sets.append("contact_id = ?")
            vals.append(nw_opt_int(data.get("contact_id")))
        for f in ("external_name", "role", "focus", "note"):
            if f in data:
                sets.append(f"{f} = ?")
                vals.append(data.get(f) or "")
        if sets:
            vals.append(aid)
            conn.execute(f"UPDATE nw_advisors SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_catalogs", tab="advisors"))


@app.get("/networking/assistants")
def networking_assistants():
    conn = get_db()
    gatherings = [dict(r) for r in conn.execute("SELECT * FROM nw_gatherings ORDER BY created_at DESC")]
    for g in gatherings:
        guests = [
            dict(x)
            for x in conn.execute(
                """
                SELECT gg.*, c.display_name FROM nw_gathering_guests gg
                LEFT JOIN nw_contacts c ON c.id = gg.contact_id
                WHERE gg.gathering_id = ?
                """,
                (g["id"],),
            )
        ]
        n = len(guests) or 1
        new_n = sum(1 for x in guests if x.get("is_new_face"))
        star_n = sum(1 for x in guests if x.get("is_star_guest"))
        g["guests"] = guests
        g["new_pct"] = round(100 * new_n / max(1, len(guests)), 1) if guests else 0
        g["star_n"] = star_n
        g["warn_new"] = (new_n / n) > 0.25 if guests else False
        g["warn_star"] = star_n == 0 and bool(guests)
    diagnoses = [
        dict(r)
        for r in conn.execute(
            """
            SELECT d.*, c.display_name FROM nw_diagnoses d
            JOIN nw_contacts c ON c.id = d.contact_id
            ORDER BY d.taken_at DESC LIMIT 20
            """
        )
    ]
    reviews = [dict(r) for r in conn.execute("SELECT * FROM nw_reviews ORDER BY period DESC")]
    return render_template(
        "networking_assistants.html",
        contacts=nw_select_contacts(conn),
        gatherings=gatherings,
        diagnoses=diagnoses,
        reviews=reviews,
        tab=request.args.get("tab") or "ois",
        first_draft=nw_get_settings(conn).get("first_contact_draft") or "",
    )


@app.get("/networking/assistants/review")
def networking_review():
    conn = get_db()
    period = request.args.get("period") or local_today().strftime("%Y-%m")
    row = conn.execute("SELECT * FROM nw_reviews WHERE period = ?", (period,)).fetchone()
    review = dict(row) if row else {"period": period, "step1_fit": "", "step2_changes": "", "step3_plan": ""}
    key_contacts = [
        nw_enrich_contact(conn, dict(r))
        for r in conn.execute(
            "SELECT * FROM nw_contacts WHERE archived_at IS NULL AND is_key = 1 ORDER BY display_name"
        )
    ]
    problems = nw_open_problem_ids(conn)
    problem_contacts = [
        nw_enrich_contact(conn, dict(r))
        for r in conn.execute("SELECT * FROM nw_contacts WHERE archived_at IS NULL ORDER BY display_name")
        if r["id"] in problems
    ]
    actions = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM nw_actions WHERE source = 'review' AND note = ? ORDER BY id",
            (period,),
        )
    ]
    return render_template(
        "networking_review.html",
        review=review,
        key_contacts=key_contacts,
        problem_contacts=problem_contacts,
        actions=actions,
        contacts=nw_select_contacts(conn),
        period=period,
    )


@app.post("/networking/reviews")
def networking_review_save():
    conn = get_db()
    period = (nw_get("period") or local_today().strftime("%Y-%m")).strip()
    now = iso(utc_now())
    existing = conn.execute("SELECT id FROM nw_reviews WHERE period = ?", (period,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE nw_reviews SET step1_fit = ?, step2_changes = ?, step3_plan = ?, updated_at = ? WHERE id = ?",
            (nw_get("step1_fit") or "", nw_get("step2_changes") or "", nw_get("step3_plan") or "", now, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO nw_reviews (period, step1_fit, step2_changes, step3_plan, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (period, nw_get("step1_fit") or "", nw_get("step2_changes") or "", nw_get("step3_plan") or "", now, now),
        )
    titles = nw_getlist("action_title")
    dues = nw_getlist("action_due")
    cids = nw_getlist("action_contact")
    for i, title in enumerate(titles):
        title = (title or "").strip()
        if not title:
            continue
        due = dues[i] if i < len(dues) else ""
        cid = nw_opt_int(cids[i]) if i < len(cids) else None
        conn.execute(
            "INSERT INTO nw_actions (title, contact_id, due_date, status, source, note, created_at) VALUES (?, ?, ?, 'open', 'review', ?, ?)",
            (title, cid, due or None, period, now),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_review", period=period))


@app.post("/networking/diagnoses")
def networking_diagnosis_create():
    conn = get_db()
    cid = nw_opt_int(nw_get("contact_id"))
    if not cid:
        return nw_err("Выберите контакт")
    verdict = nw_get("verdict") or "keep"
    conn.execute(
        """
        INSERT INTO nw_diagnoses (contact_id, taken_at, flag_reschedules, flag_no_initiative, flag_no_openness, flag_sudden_interest, verdict, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            iso(utc_now()),
            nw_flag(nw_get("flag_reschedules")),
            nw_flag(nw_get("flag_no_initiative")),
            nw_flag(nw_get("flag_no_openness")),
            nw_flag(nw_get("flag_sudden_interest")),
            verdict,
            nw_get("note") or "",
        ),
    )
    c = nw_contact_or_404(conn, cid)
    titles = {
        "talk": f"Обсудить отношения с {c['display_name']}",
        "small_ask_test": f"Небольшая просьба к {c['display_name']}",
        "reduce": f"Снизить вложение в связь с {c['display_name']}",
        "release": f"Отпустить контакт {c['display_name']}",
    }
    if verdict in titles:
        conn.execute(
            "INSERT INTO nw_actions (title, contact_id, due_date, status, source, note, created_at) VALUES (?, ?, ?, 'open', 'diagnosis', ?, ?)",
            (titles[verdict], cid, (local_today() + timedelta(days=7)).isoformat(), verdict, iso(utc_now())),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_assistants", tab="diag"))


@app.post("/networking/gatherings")
def networking_gathering_create():
    conn = get_db()
    title = (nw_get("title") or "").strip()
    if not title:
        return nw_err("Укажите название")
    conn.execute(
        "INSERT INTO nw_gatherings (title, planned_at, place, goal, notes, status, created_at) VALUES (?, ?, ?, ?, ?, 'planned', ?)",
        (title, nw_get("planned_at") or None, nw_get("place") or "", nw_get("goal") or "", nw_get("notes") or "", iso(utc_now())),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_assistants", tab="party"))


@app.post("/networking/gatherings/<int:gid>")
def networking_gathering_update(gid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_gatherings WHERE id = ?", (gid,))
    else:
        data = nw_payload()
        sets, vals = [], []
        for f in ("title", "planned_at", "place", "goal", "notes", "status"):
            if f in data:
                sets.append(f"{f} = ?")
                vals.append(data.get(f) or "")
        if sets:
            vals.append(gid)
            conn.execute(f"UPDATE nw_gatherings SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_assistants", tab="party"))


@app.post("/networking/gatherings/<int:gid>/guests")
def networking_gathering_guest_add(gid: int):
    conn = get_db()
    if not conn.execute("SELECT id FROM nw_gatherings WHERE id = ?", (gid,)).fetchone():
        abort(404)
    conn.execute(
        """
        INSERT INTO nw_gathering_guests (gathering_id, contact_id, external_name, is_new_face, is_star_guest, rsvp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            gid,
            nw_opt_int(nw_get("contact_id")),
            nw_get("external_name") or "",
            nw_flag(nw_get("is_new_face")),
            nw_flag(nw_get("is_star_guest")),
            nw_get("rsvp") or "invited",
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_assistants", tab="party"))


@app.post("/networking/gathering-guests/<int:gsid>/delete")
def networking_gathering_guest_delete(gsid: int):
    conn = get_db()
    conn.execute("DELETE FROM nw_gathering_guests WHERE id = ?", (gsid,))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_assistants", tab="party"))


@app.post("/networking/actions")
def networking_action_create():
    conn = get_db()
    title = (nw_get("title") or "").strip()
    if not title:
        return nw_err("Укажите задачу")
    conn.execute(
        "INSERT INTO nw_actions (title, contact_id, due_date, status, source, note, created_at) VALUES (?, ?, ?, 'open', ?, ?, ?)",
        (
            title,
            nw_opt_int(nw_get("contact_id")),
            nw_get("due_date") or None,
            nw_get("source") or "manual",
            nw_get("note") or "",
            iso(utc_now()),
        ),
    )
    conn.commit()
    dest = nw_get("next") or url_for("networking_home")
    if isinstance(dest, str) and dest.startswith("/"):
        return nw_ok({"ok": True}, dest)
    return nw_ok({"ok": True}, url_for("networking_home"))


@app.post("/networking/actions/<int:aid>")
def networking_action_update(aid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_actions WHERE id = ?", (aid,))
    elif nw_get("status") == "done":
        conn.execute("UPDATE nw_actions SET status = 'done', done_at = ? WHERE id = ?", (iso(utc_now()), aid))
    elif nw_get("status") == "dropped":
        conn.execute("UPDATE nw_actions SET status = 'dropped' WHERE id = ?", (aid,))
    else:
        conn.execute(
            "UPDATE nw_actions SET title = ?, due_date = ?, note = ? WHERE id = ?",
            (nw_get("title") or "", nw_get("due_date") or None, nw_get("note") or "", aid),
        )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_home"))


@app.post("/networking/first-contact")
def networking_first_contact():
    conn = get_db()
    cid = nw_opt_int(nw_get("contact_id"))
    cover = (nw_get("cover") or "").strip()
    if cid and cover:
        c = nw_contact_or_404(conn, cid)
        notes = (c.get("notes") or "").rstrip()
        block = "Ситуативное прикрытие:\n" + cover
        conn.execute(
            "UPDATE nw_contacts SET notes = ?, updated_at = ? WHERE id = ?",
            (((notes + "\n\n") if notes else "") + block, iso(utc_now()), cid),
        )
        conn.execute(
            "INSERT INTO nw_actions (title, contact_id, due_date, status, source, note, created_at) VALUES (?, ?, ?, 'open', 'manual', ?, ?)",
            (
                f"Написать {c['display_name']} завтра утром",
                cid,
                (local_today() + timedelta(days=1)).isoformat(),
                "правило 24–48 часов",
                iso(utc_now()),
            ),
        )
        conn.commit()
        return nw_ok({"ok": True}, url_for("networking_contact", contact_id=cid))
    if cover:
        nw_set_setting(conn, "first_contact_draft", cover)
        conn.commit()
    return nw_ok({"ok": True}, url_for("networking_assistants", tab="first"))


@app.get("/networking/settings")
def networking_settings():
    conn = get_db()
    return render_template(
        "networking_settings.html",
        settings=nw_get_settings(conn),
        sectors=[dict(r) for r in conn.execute("SELECT * FROM nw_sectors ORDER BY sort_order, id")],
        groups=[dict(r) for r in conn.execute("SELECT * FROM nw_interest_groups ORDER BY sort_order, id")],
        goal_sectors=[dict(r) for r in conn.execute("SELECT * FROM nw_goal_sectors ORDER BY sort_order, id")],
        holidays=[dict(r) for r in conn.execute("SELECT * FROM nw_dates WHERE contact_id IS NULL ORDER BY month, day")],
    )


@app.post("/networking/settings")
def networking_settings_save():
    conn = get_db()
    settings = nw_get_settings(conn)
    data = nw_payload()
    for k in (
        "cadence_list_1",
        "cadence_list_2",
        "cadence_list_3",
        "birthday_lead_days",
        "target_support",
        "target_productivity",
        "target_development",
        "churn_target_pct",
    ):
        if k in data:
            nw_set_setting(conn, k, str(data.get(k) or "").strip())
    if "cadence_list_1" in data:
        nw_set_setting(conn, "mask_confidential", "1" if nw_flag(data.get("mask_confidential")) else "0")
    pin_action = nw_get("pin_action") or ""
    pin = (nw_get("pin") or "").strip()
    pin2 = (nw_get("pin_confirm") or "").strip()
    current = (nw_get("pin_current") or "").strip()
    if pin_action == "set":
        if len(pin) < 4:
            return nw_err("PIN не короче 4 символов")
        if pin != pin2:
            return nw_err("PIN и подтверждение не совпадают")
        if settings.get("pin_hash"):
            if not current or nw_hash_pin(current, settings.get("pin_salt") or "") != settings.get("pin_hash"):
                return nw_err("Неверный текущий PIN")
        salt = secrets.token_hex(16)
        nw_set_setting(conn, "pin_salt", salt)
        nw_set_setting(conn, "pin_hash", nw_hash_pin(pin, salt))
        nw_set_setting(conn, "lock_enabled", "1")
        session["nw_unlocked"] = True
    elif pin_action == "clear":
        if settings.get("pin_hash"):
            if not current or nw_hash_pin(current, settings.get("pin_salt") or "") != settings.get("pin_hash"):
                return nw_err("Неверный текущий PIN")
        nw_set_setting(conn, "pin_hash", "")
        nw_set_setting(conn, "pin_salt", "")
        nw_set_setting(conn, "lock_enabled", "0")
        session.pop("nw_unlocked", None)
    elif "lock_enabled" in data:
        want = nw_flag(data.get("lock_enabled"))
        fresh = nw_get_settings(conn)
        if want and not fresh.get("pin_hash"):
            return nw_err("Сначала задайте PIN")
        nw_set_setting(conn, "lock_enabled", "1" if want else "0")
        if not want:
            session.pop("nw_unlocked", None)
    nw_recompute_all_dues(conn)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_settings"))


@app.post("/networking/settings/recompute-due")
def networking_recompute_due():
    conn = get_db()
    nw_recompute_all_dues(conn)
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_settings"))


@app.post("/networking/sectors")
def networking_sector_create():
    conn = get_db()
    name = (nw_get("name") or "").strip()
    if not name:
        return nw_err("Укажите название")
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM nw_sectors").fetchone()[0]
    conn.execute("INSERT INTO nw_sectors (name, sort_order) VALUES (?, ?)", (name, max_order + 1))
    conn.commit()
    dest = url_for("networking_map") if nw_get("from") == "map" else url_for("networking_settings")
    return nw_ok({"ok": True}, dest)


@app.post("/networking/sectors/<int:sid>")
def networking_sector_update(sid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_sectors WHERE id = ?", (sid,))
    elif nw_get("move") in ("up", "down"):
        row = conn.execute("SELECT * FROM nw_sectors WHERE id = ?", (sid,)).fetchone()
        if row:
            if nw_get("move") == "up":
                other = conn.execute(
                    "SELECT * FROM nw_sectors WHERE sort_order < ? ORDER BY sort_order DESC LIMIT 1",
                    (row["sort_order"],),
                ).fetchone()
            else:
                other = conn.execute(
                    "SELECT * FROM nw_sectors WHERE sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                    (row["sort_order"],),
                ).fetchone()
            if other:
                conn.execute("UPDATE nw_sectors SET sort_order = ? WHERE id = ?", (other["sort_order"], sid))
                conn.execute("UPDATE nw_sectors SET sort_order = ? WHERE id = ?", (row["sort_order"], other["id"]))
    else:
        conn.execute("UPDATE nw_sectors SET name = ? WHERE id = ?", ((nw_get("name") or "").strip(), sid))
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_settings"))


@app.post("/networking/interest-groups")
def networking_group_create():
    conn = get_db()
    name = (nw_get("name") or "").strip()
    if not name:
        return nw_err("Укажите название")
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM nw_interest_groups").fetchone()[0]
    conn.execute(
        "INSERT INTO nw_interest_groups (name, color, sort_order) VALUES (?, ?, ?)",
        (name, nw_get("color") or "#6aa9ff", max_order + 1),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_settings"))


@app.post("/networking/interest-groups/<int:gid>")
def networking_group_update(gid: int):
    conn = get_db()
    if nw_flag(nw_get("delete")):
        conn.execute("DELETE FROM nw_interest_groups WHERE id = ?", (gid,))
    else:
        row = conn.execute("SELECT * FROM nw_interest_groups WHERE id = ?", (gid,)).fetchone()
        if row:
            conn.execute(
                "UPDATE nw_interest_groups SET name = ?, color = ? WHERE id = ?",
                ((nw_get("name") or row["name"]).strip(), nw_get("color") or row["color"], gid),
            )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_settings"))


@app.post("/networking/holidays")
def networking_holiday_create():
    conn = get_db()
    month = nw_int(nw_get("month"), 0, 1, 12)
    day = nw_int(nw_get("day"), 0, 1, 31)
    if not month or not day:
        return nw_err("Укажите дату")
    settings = nw_get_settings(conn)
    conn.execute(
        """
        INSERT INTO nw_dates (contact_id, kind, title, month, day, year, recurring, lead_days, greeting_note, active, created_at)
        VALUES (NULL, 'holiday', ?, ?, ?, NULL, 1, ?, ?, 1, ?)
        """,
        (
            (nw_get("title") or "").strip() or "Праздник",
            month,
            day,
            nw_get("lead_days") or settings.get("birthday_lead_days") or "7,1,0",
            nw_get("greeting_note") or "",
            iso(utc_now()),
        ),
    )
    conn.commit()
    return nw_ok({"ok": True}, url_for("networking_settings"))


@app.get("/networking/import")
def networking_import():
    conn = get_db()
    contacts_count = conn.execute(
        "SELECT COUNT(*) FROM nw_contacts WHERE archived_at IS NULL"
    ).fetchone()[0]
    return render_template(
        "networking_import.html",
        contacts_count=contacts_count,
    )


@app.get("/networking/import/template.csv")
def networking_import_template():
    return nw_contacts_template_csv(filled=False)


@app.get("/networking/import/template-filled.csv")
def networking_import_template_filled():
    return nw_contacts_template_csv(filled=True)


@app.post("/networking/import/preview")
def networking_import_preview():
    f = request.files.get("file")
    if not f or not f.filename:
        return nw_err("Выберите файл")
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    name = f.filename
    session["nw_import_text"] = text
    session["nw_import_name"] = name
    if name.lower().endswith(".vcf") or "BEGIN:VCARD" in text.upper():
        cards = parse_vcard_text(text)
        return jsonify({"ok": True, "kind": "vcf", "filename": name, "headers": [], "rows": cards, "mapping": {}})
    headers, rows = parse_csv_contacts(text)
    return jsonify(
        {
            "ok": True,
            "kind": "csv",
            "filename": name,
            "headers": headers,
            "rows": rows[:80],
            "total": len(rows),
            "mapping": guess_csv_mapping(headers),
            "fields": [
                "display_name",
                "first_name",
                "last_name",
                "phones",
                "emails",
                "company",
                "position",
                "city",
                "notes",
                "birth_date",
                "tags",
            ],
        }
    )


@app.post("/networking/import/commit")
def networking_import_commit():
    conn = get_db()
    data = request.get_json(silent=True) or {}
    text = session.get("nw_import_text") or ""
    filename = session.get("nw_import_name") or "import"
    if not text:
        return nw_err("Нет данных для импорта — сначала предпросмотр")
    kind = data.get("kind") or "csv"
    mapping = data.get("mapping") or {}
    decisions = data.get("decisions") or {}
    created = updated = skipped = 0
    now = iso(utc_now())
    if kind == "vcf":
        records = parse_vcard_text(text)
    else:
        _headers, rows = parse_csv_contacts(text)
        records = []
        for row in rows:
            rec = {}
            for src, dst in mapping.items():
                if dst and src in row:
                    rec[dst] = row.get(src) or ""
            records.append(rec)
    for i, rec in enumerate(records):
        rec = dict(rec)
        rec["display_name"] = (rec.get("display_name") or "").strip() or (
            ((rec.get("first_name") or "") + " " + (rec.get("last_name") or "")).strip()
        )
        if not rec["display_name"]:
            skipped += 1
            continue
        rec.setdefault("circle", "development")
        rec.setdefault("priority_list", "3")
        rec.setdefault("stage", "new")
        rec["import_source"] = filename
        rec["imported_at"] = now
        dup = nw_find_duplicate(conn, rec["display_name"], rec.get("phones") or "", rec.get("emails") or "")
        decision = decisions.get(str(i)) or ("skip" if dup else "create")
        if dup and decision == "skip":
            skipped += 1
            continue
        if dup and decision == "update":
            fields = nw_apply_contact_payload(rec, dup)
            for k in (
                "phones",
                "emails",
                "company",
                "position",
                "city",
                "notes",
                "birth_date",
                "first_name",
                "last_name",
            ):
                if not (dup.get(k) or "").strip() and (rec.get(k) or "").strip():
                    fields[k] = rec[k]
            nw_update_contact_row(conn, dup["id"], fields)
            updated += 1
            continue
        fields = nw_apply_contact_payload(rec)
        fields["import_source"] = filename
        fields["imported_at"] = now
        nw_insert_contact(conn, fields)
        created += 1
    conn.commit()
    session.pop("nw_import_text", None)
    session.pop("nw_import_name", None)
    return jsonify({"ok": True, "created": created, "updated": updated, "skipped": skipped})


@app.get("/networking/export/contacts.csv")
def networking_export_csv():
    conn = get_db()
    include = request.args.get("confidential") == "1"
    mask = nw_mask_on(conn) and not include
    rows = [dict(r) for r in conn.execute("SELECT * FROM nw_contacts ORDER BY display_name COLLATE NOCASE")]
    cols = [
        "display_name",
        "first_name",
        "last_name",
        "nickname",
        "circle",
        "priority_list",
        "stage",
        "roles",
        "company",
        "position",
        "city",
        "phones",
        "emails",
        "birth_date",
        "tags",
        "notes",
        "last_contact_at",
        "next_contact_due",
    ]
    if include:
        cols.append("private_notes")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for c in rows:
        if c.get("is_confidential") and mask:
            w.writerow(["Скрытый контакт"] + [""] * (len(cols) - 1))
            continue
        vals = []
        for col in cols:
            if col in ("notes", "private_notes") and c.get("is_confidential") and not include:
                vals.append("")
            else:
                vals.append(c.get(col) or "")
        w.writerow(vals)
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )


@app.get("/networking/export/network.md")
def networking_export_md():
    conn = get_db()
    include = request.args.get("confidential") == "1"
    parts = ["# Сеть", ""]
    for key, label in NW_CIRCLES.items():
        parts.append(f"## {label}")
        parts.append("")
        rows = conn.execute(
            "SELECT * FROM nw_contacts WHERE archived_at IS NULL AND circle = ? ORDER BY display_name COLLATE NOCASE",
            (key,),
        ).fetchall()
        if not rows:
            parts.append("_пусто_")
            parts.append("")
            continue
        for r in rows:
            c = dict(r)
            if c.get("is_confidential") and not include:
                parts.append("- Скрытый контакт")
                parts.append("")
                continue
            parts.append(nw_contact_md(conn, c, include))
            parts.append("")
    return Response(
        "\n".join(parts),
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="network.md"'},
    )


@app.get("/networking/export/contacts/<int:contact_id>.md")
def networking_export_contact_md(contact_id: int):
    conn = get_db()
    c = nw_contact_or_404(conn, contact_id)
    include = request.args.get("confidential") == "1"
    body = nw_contact_md(conn, c, include)
    filename = _safe_report_filename(c["display_name"], "md")
    return Response(
        body,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def main():
    init_db()
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7878"))
    app.run(debug=debug, host=host, port=port)


if __name__ == "__main__":
    main()
