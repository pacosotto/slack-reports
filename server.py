#!/usr/bin/env python3
"""
server.py — Mini servidor Flask para el dashboard de reportes.

Uso:
    python server.py
    Luego abre http://localhost:5000 en tu browser.

Requisitos:
    pip install flask
"""

import sqlite3
import json
import os
import re
import time
import subprocess
import urllib.request
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory

# ─── Cargar .env ──────────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

DB_PATH      = os.path.join(os.path.dirname(__file__), "reports.db")
STATIC_PATH  = os.path.join(os.path.dirname(__file__), "dashboard")
REPORTS_PY   = os.path.join(os.path.dirname(__file__), "reports.py")
SLACK_TOKEN  = os.getenv("SLACK_TOKEN", "")

_sync_process = None  # referencia al proceso en background

app = Flask(__name__, static_folder=STATIC_PATH)


# ─── Slack ────────────────────────────────────────────────────────────────────

_channel_id_cache: dict = {}   # nombre → channel_id
_user_name_cache:  dict = {}   # user_id → nombre real


def _slack_get(path: str) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{path}",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def slack_resolve_channel_id(channel_name: str) -> str:
    if channel_name in _channel_id_cache:
        return _channel_id_cache[channel_name]
    data = _slack_get("conversations.list?limit=200&types=public_channel,private_channel")
    for ch in data.get("channels", []):
        _channel_id_cache[ch["name"]] = ch["id"]
    return _channel_id_cache.get(channel_name, "")


def slack_resolve_user_name(user_id: str) -> str:
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    data = _slack_get(f"users.info?user={user_id}")
    user = data.get("user", {})
    name = user.get("real_name") or user.get("name") or user_id
    _user_name_cache[user_id] = name
    return name


def slack_post_message(channel: str, text: str, thread_ts: str = None) -> dict:
    """Envía un mensaje a Slack, opcionalmente en un hilo. Retorna la respuesta."""
    if not SLACK_TOKEN:
        return {"ok": False, "error": "no_token"}
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tasks_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id                  TEXT PRIMARY KEY,
            description         TEXT,
            title               TEXT,
            task_type           TEXT,
            priority            TEXT,
            estimated_minutes   INTEGER,
            estimate_reason     TEXT DEFAULT '',
            status              TEXT DEFAULT 'pendiente',
            actual_time_minutes INTEGER,
            resolution_notes    TEXT DEFAULT '',
            deleted             INTEGER DEFAULT 0,
            completed_at        INTEGER,
            created_at          INTEGER,
            updated_at          INTEGER
        )
    """)
    conn.commit()
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN completed_at INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


def init_accounts_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            username    TEXT DEFAULT '',
            status      TEXT DEFAULT 'activa',
            notes       TEXT DEFAULT '',
            created_at  INTEGER,
            updated_at  INTEGER
        )
    """)
    conn.commit()
    for table in ("reports", "tasks"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()


def init_reports_migrations():
    """Migra la tabla reports: agrega completed_at y corrige created_at desde slack_ts."""
    conn = get_db()
    try:
        conn.execute("ALTER TABLE reports ADD COLUMN completed_at INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        UPDATE reports
        SET created_at = CAST(CAST(slack_ts AS REAL) AS INTEGER)
        WHERE slack_ts IS NOT NULL AND slack_ts != '' AND CAST(slack_ts AS REAL) > 0
    """)
    conn.commit()
    conn.close()


def call_claude(prompt: str) -> str:
    result = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_resolution_history(limit: int = 30) -> list:
    """Historial de reportes y tareas resueltas con tiempo real, para contexto de estimación."""
    conn = get_db()
    history = []
    for row in conn.execute("""
        SELECT report_type as type, summary as title, resolution_time_minutes as minutes
        FROM reports
        WHERE status = 'resuelto' AND resolution_time_minutes IS NOT NULL
          AND COALESCE(deleted, 0) = 0
        ORDER BY updated_at DESC LIMIT ?
    """, (limit,)):
        history.append(dict(row))
    try:
        for row in conn.execute("""
            SELECT task_type as type, title, actual_time_minutes as minutes
            FROM tasks
            WHERE status = 'completada' AND actual_time_minutes IS NOT NULL
              AND COALESCE(deleted, 0) = 0
            ORDER BY updated_at DESC LIMIT ?
        """, (limit,)):
            history.append(dict(row))
    except Exception:
        pass
    conn.close()
    return history


def init_settings():
    """Crea la tabla settings si no existe."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_setting(key: str) -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_linkaform_jwt() -> str:
    return get_setting("linkaform_jwt")


def row_to_dict(row) -> dict:
    d = dict(row)
    # Deserializar JSON strings
    for field in ("key_points", "linkaform_data"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/reports")
def list_reports():
    """Lista reportes con filtros opcionales."""
    status    = request.args.get("status")
    priority  = request.args.get("priority")
    rtype     = request.args.get("type")
    limit     = int(request.args.get("limit", 100))

    conn = get_db()
    query = "SELECT * FROM reports WHERE COALESCE(deleted, 0) = 0"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if rtype:
        query += " AND report_type = ?"
        params.append(rtype)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/reports/<report_id>")
def get_report(report_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(row))


@app.route("/api/reports/<report_id>/status", methods=["PATCH"])
def update_status(report_id):
    """Actualiza el status de un reporte: nuevo | en_proceso | resuelto | ignorado"""
    body = request.get_json()
    new_status = body.get("status")
    valid = {"nuevo", "visto", "en_revision", "en_proceso", "resuelto"}
    if new_status not in valid:
        return jsonify({"error": f"status debe ser uno de: {valid}"}), 400

    conn = get_db()

    # Leer el reporte antes de actualizar para tener canal y ts
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()

    now = int(time.time())
    if new_status == "resuelto":
        conn.execute(
            "UPDATE reports SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
            (new_status, now, now, report_id)
        )
    else:
        conn.execute(
            "UPDATE reports SET status = ?, updated_at = ?, completed_at = NULL WHERE id = ?",
            (new_status, now, report_id)
        )
    conn.commit()

    conn.close()
    return jsonify({"ok": True, "status": new_status})


NOTIFY_MESSAGES = {
    "visto":       "👀 Paco ya vio el reporte, pronto comenzará a revisarlo...",
    "en_revision": "🔍 Paco está revisando el reporte...",
    "en_proceso":  "⚙️ Paco ya está trabajando en ello...",
    "resuelto":    "✅ El reporte ha sido resuelto, quedo atento a cualquier comentario.",
}

@app.route("/api/reports/<report_id>/notify", methods=["POST"])
def notify(report_id):
    """Envía notificación a Slack según el status indicado."""
    body   = request.get_json() or {}
    status = body.get("status")

    if status not in NOTIFY_MESSAGES:
        return jsonify({"error": f"status inválido: {status}"}), 400

    conn = get_db()
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404
    if not SLACK_TOKEN:
        return jsonify({"error": "SLACK_TOKEN no configurado"}), 500

    result = slack_post_message(
        channel=row["slack_channel"],
        text=NOTIFY_MESSAGES[status],
        thread_ts=row["slack_ts"],
    )
    print(f"  Slack notify [{status}] → channel={row['slack_channel']} result={result}")
    return jsonify({"ok": result.get("ok", False), "slack": result})


@app.route("/api/reports/<report_id>", methods=["DELETE"])
def delete_report(report_id):
    """Soft-delete: marca el reporte como eliminado para que no reaparezca en syncs."""
    conn = get_db()
    conn.execute(
        "UPDATE reports SET deleted = 1, updated_at = ? WHERE id = ?",
        (int(time.time()), report_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/reports/<report_id>/thread")
def get_thread(report_id):
    """Trae las respuestas del hilo de Slack para un reporte."""
    conn = get_db()
    row = conn.execute("SELECT slack_channel, slack_ts FROM reports WHERE id = ?", (report_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404

    if not SLACK_TOKEN:
        return jsonify({"messages": [], "error": "no_token"})

    channel_id = slack_resolve_channel_id(row["slack_channel"])
    if not channel_id:
        return jsonify({"messages": [], "error": f"canal '{row['slack_channel']}' no encontrado"})

    data = _slack_get(f"conversations.replies?channel={channel_id}&ts={row['slack_ts']}&limit=100")
    messages = data.get("messages", [])
    # El primer elemento es el mensaje padre, ya mostrado en el dashboard
    replies = messages[1:] if len(messages) > 1 else []

    for msg in replies:
        uid = msg.get("user", "")
        msg["user_name"] = slack_resolve_user_name(uid) if uid else msg.get("username", "?")

    return jsonify({"messages": replies, "count": len(replies)})


@app.route("/api/reports/<report_id>/resolution", methods=["PATCH"])
def update_resolution(report_id):
    """Guarda el tiempo y causa de resolución, y marca el reporte como resuelto."""
    body = request.get_json() or {}
    time_minutes = body.get("resolution_time_minutes")
    cause = body.get("resolution_cause", "")
    now = int(time.time())
    conn = get_db()
    conn.execute(
        "UPDATE reports SET resolution_time_minutes = ?, resolution_cause = ?, status = 'resuelto', updated_at = ?, completed_at = ? WHERE id = ?",
        (time_minutes, cause, now, now, report_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/reports/<report_id>/notes", methods=["PATCH"])
def update_notes(report_id):
    """Actualiza las notas de un reporte."""
    body = request.get_json()
    notes = body.get("notes", "")
    conn = get_db()
    conn.execute(
        "UPDATE reports SET notes = ?, updated_at = ? WHERE id = ?",
        (notes, int(time.time()), report_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/stats")
def stats():
    """Estadísticas generales para el dashboard."""
    conn = get_db()

    f = "COALESCE(deleted, 0) = 0"
    total     = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {f}").fetchone()[0]
    nuevos    = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {f} AND status = 'nuevo'").fetchone()[0]
    vistos      = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {f} AND status = 'visto'").fetchone()[0]
    en_revision = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {f} AND status = 'en_revision'").fetchone()[0]
    en_proceso  = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {f} AND status = 'en_proceso'").fetchone()[0]
    resueltos  = conn.execute(f"SELECT COUNT(*) FROM reports WHERE {f} AND status = 'resuelto'").fetchone()[0]

    por_tipo = {}
    for row in conn.execute(f"SELECT report_type, COUNT(*) as n FROM reports WHERE {f} GROUP BY report_type"):
        por_tipo[row[0]] = row[1]

    por_prioridad = {}
    for row in conn.execute(f"SELECT priority, COUNT(*) as n FROM reports WHERE {f} GROUP BY priority"):
        por_prioridad[row[0]] = row[1]

    conn.close()

    return jsonify({
        "total": total,
        "nuevos": nuevos,
        "vistos": vistos,
        "en_revision": en_revision,
        "en_proceso": en_proceso,
        "resueltos": resueltos,
        "por_tipo": por_tipo,
        "por_prioridad": por_prioridad,
    })


# ─── Auth Linkaform ───────────────────────────────────────────────────────────

@app.route("/api/auth/status")
def auth_status():
    jwt    = get_linkaform_jwt()
    user   = get_setting("linkaform_user")
    avatar = get_setting("linkaform_avatar")
    return jsonify({"logged_in": bool(jwt), "user": user, "avatar": avatar})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body     = request.get_json() or {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    if not username or not password:
        return jsonify({"ok": False, "error": "Usuario y contraseña requeridos"}), 400

    payload = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        "https://app.linkaform.com/api/infosync/user_admin/login/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        return jsonify({"ok": False, "error": f"HTTP {e.code}: {body_err}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    jwt = result.get("jwt") or result.get("jwt_complete") or result.get("token", "")
    if not jwt:
        return jsonify({"ok": False, "error": "Credenciales incorrectas", "detail": result}), 401

    user_data = result.get("user", {})
    name      = user_data.get("name") or user_data.get("first_name") or username
    avatar    = user_data.get("profile_picture") or user_data.get("thumb", "")

    set_setting("linkaform_jwt",    jwt)
    set_setting("linkaform_user",   name)
    set_setting("linkaform_avatar", avatar)

    return jsonify({"ok": True, "user": name, "avatar": avatar})


@app.route("/api/auth/avatar")
def auth_avatar():
    """Descarga el avatar desde Linkaform/Backblaze y lo sirve como proxy."""
    avatar_url = get_setting("linkaform_avatar")
    jwt        = get_linkaform_jwt()

    if not avatar_url:
        return "", 404

    req = urllib.request.Request(
        avatar_url,
        headers={"Authorization": f"Bearer {jwt}"} if jwt else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data         = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
    except Exception:
        return "", 404

    from flask import Response
    return Response(data, content_type=content_type)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    set_setting("linkaform_jwt", "")
    set_setting("linkaform_user", "")
    return jsonify({"ok": True})


# ─── Tasks ────────────────────────────────────────────────────────────────────

@app.route("/api/tasks/stats")
def tasks_stats():
    f = "COALESCE(deleted, 0) = 0"
    conn = get_db()
    total      = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {f}").fetchone()[0]
    pendientes = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {f} AND status = 'pendiente'").fetchone()[0]
    en_proceso = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {f} AND status = 'en_proceso'").fetchone()[0]
    completadas = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {f} AND status = 'completada'").fetchone()[0]
    conn.close()
    return jsonify({"total": total, "pendientes": pendientes, "en_proceso": en_proceso, "completadas": completadas})


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    status = request.args.get("status")
    conn = get_db()
    query = "SELECT * FROM tasks WHERE COALESCE(deleted, 0) = 0"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT 200"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tasks", methods=["POST"])
def create_task():
    body = request.get_json() or {}
    description = body.get("description", "").strip()
    if not description:
        return jsonify({"error": "description requerida"}), 400

    history = get_resolution_history()
    if history:
        history_str = "\n".join(
            f"- [{h['type']}] {h['title']}: {h['minutes']} minutos"
            for h in history if h.get("minutes")
        )
    else:
        history_str = "Sin historial disponible aún."

    prompt = f"""Eres un asistente experto en gestión de tareas de tecnología y software.

DESCRIPCIÓN DE LA NUEVA TAREA:
{description}

HISTORIAL DE TAREAS Y REPORTES RESUELTOS (tipo · título · tiempo real invertido):
{history_str}

Analiza la descripción y el historial. Genera lo siguiente.
Responde ÚNICAMENTE con un objeto JSON válido, sin backticks ni texto adicional:
{{
  "title": "Título conciso de la tarea (máx 70 caracteres)",
  "task_type": "bug|tarea|config|revisión|investigación|otro",
  "priority": "alta|media|baja",
  "estimated_minutes": 60,
  "estimate_reason": "Una oración explicando el estimado y en qué se basó"
}}

Si el historial es escaso, usa criterio general de industria. Sé conservador y realista."""

    raw = call_claude(prompt)
    raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except Exception:
        data = {"title": description[:70], "task_type": "tarea", "priority": "media",
                "estimated_minutes": 60, "estimate_reason": "Estimación por defecto"}

    now = int(time.time())
    task_id = f"task_{now}_{abs(hash(description)) % 9999:04d}"

    conn = get_db()
    conn.execute("""
        INSERT INTO tasks (id, description, title, task_type, priority, estimated_minutes,
                           estimate_reason, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pendiente', ?, ?)
    """, (task_id, description,
          data.get("title", description[:70]),
          data.get("task_type", "tarea"),
          data.get("priority", "media"),
          data.get("estimated_minutes", 60),
          data.get("estimate_reason", ""),
          now, now))
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route("/api/tasks/<task_id>/status", methods=["PATCH"])
def update_task_status(task_id):
    body = request.get_json() or {}
    new_status = body.get("status")
    if new_status not in {"pendiente", "en_proceso", "completada"}:
        return jsonify({"error": "status inválido"}), 400
    now = int(time.time())
    conn = get_db()
    if new_status == "completada":
        conn.execute("UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                     (new_status, now, now, task_id))
    else:
        conn.execute("UPDATE tasks SET status = ?, updated_at = ?, completed_at = NULL WHERE id = ?",
                     (new_status, now, task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/tasks/<task_id>/resolution", methods=["PATCH"])
def complete_task(task_id):
    body = request.get_json() or {}
    conn = get_db()
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET actual_time_minutes = ?, resolution_notes = ?, status = 'completada', updated_at = ?, completed_at = ? WHERE id = ?",
        (body.get("actual_time_minutes"), body.get("resolution_notes", ""), now, now, task_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    conn = get_db()
    conn.execute("UPDATE tasks SET deleted = 1, updated_at = ? WHERE id = ?",
                 (int(time.time()), task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ─── Accounts ────────────────────────────────────────────────────────────────

@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM accounts WHERE status != 'archivada' ORDER BY name"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/accounts", methods=["POST"])
def create_account():
    body = request.get_json() or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name requerido"}), 400
    now = int(time.time())
    account_id = f"acc_{now}_{abs(hash(name)) % 9999:04d}"
    conn = get_db()
    conn.execute(
        "INSERT INTO accounts (id, name, username, status, notes, created_at, updated_at) VALUES (?, ?, ?, 'activa', ?, ?, ?)",
        (account_id, name, body.get("username", ""), body.get("notes", ""), now, now)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route("/api/accounts/<account_id>", methods=["PATCH"])
def update_account(account_id):
    body = request.get_json() or {}
    fields, vals = [], []
    for f in ("name", "username", "notes", "status"):
        if f in body:
            fields.append(f"{f} = ?")
            vals.append(body[f])
    if not fields:
        return jsonify({"error": "nada que actualizar"}), 400
    fields.append("updated_at = ?")
    vals.extend([int(time.time()), account_id])
    conn = get_db()
    conn.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {"error": "not found"})


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def archive_account(account_id):
    conn = get_db()
    conn.execute("UPDATE accounts SET status = 'archivada', updated_at = ? WHERE id = ?",
                 (int(time.time()), account_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/reports/<report_id>/account", methods=["PATCH"])
def set_report_account(report_id):
    body = request.get_json() or {}
    account_id = body.get("account_id") or None
    conn = get_db()
    conn.execute("UPDATE reports SET account_id = ?, updated_at = ? WHERE id = ?",
                 (account_id, int(time.time()), report_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<task_id>/account", methods=["PATCH"])
def set_task_account(task_id):
    body = request.get_json() or {}
    account_id = body.get("account_id") or None
    conn = get_db()
    conn.execute("UPDATE tasks SET account_id = ?, updated_at = ? WHERE id = ?",
                 (account_id, int(time.time()), task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ─── Sync ─────────────────────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def sync():
    """Corre reports.py en background. Si ya hay uno corriendo, lo indica."""
    global _sync_process

    # Si el proceso anterior todavía está vivo, no lanzar otro
    if _sync_process and _sync_process.poll() is None:
        return jsonify({"ok": False, "msg": "Sync ya en curso"}), 409

    _sync_process = subprocess.Popen(
        ["python", REPORTS_PY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return jsonify({"ok": True, "pid": _sync_process.pid})


@app.route("/api/sync/status")
def sync_status():
    """Indica si el sync está corriendo o terminó."""
    if _sync_process is None:
        return jsonify({"running": False})
    running = _sync_process.poll() is None
    return jsonify({"running": running})


# ─── Servir el dashboard ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_PATH, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_PATH, path)


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_settings()
    init_tasks_db()
    init_accounts_db()
    if os.path.exists(DB_PATH):
        init_reports_migrations()
    if not os.path.exists(DB_PATH):
        print("ℹ️  Sin reportes aún — puedes correr reports.py para sincronizar desde Slack.")
    print("🚀 Dashboard corriendo en http://localhost:5000")
    app.run(debug=True, port=5000)
