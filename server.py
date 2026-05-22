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
import time
import subprocess
import urllib.request
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
    query = "SELECT * FROM reports WHERE 1=1"
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

    conn.execute(
        "UPDATE reports SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, int(time.time()), report_id)
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

    total     = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    nuevos    = conn.execute("SELECT COUNT(*) FROM reports WHERE status = 'nuevo'").fetchone()[0]
    vistos      = conn.execute("SELECT COUNT(*) FROM reports WHERE status = 'visto'").fetchone()[0]
    en_revision = conn.execute("SELECT COUNT(*) FROM reports WHERE status = 'en_revision'").fetchone()[0]
    en_proceso  = conn.execute("SELECT COUNT(*) FROM reports WHERE status = 'en_proceso'").fetchone()[0]
    resueltos  = conn.execute("SELECT COUNT(*) FROM reports WHERE status = 'resuelto'").fetchone()[0]

    por_tipo = {}
    for row in conn.execute("SELECT report_type, COUNT(*) as n FROM reports GROUP BY report_type"):
        por_tipo[row[0]] = row[1]

    por_prioridad = {}
    for row in conn.execute("SELECT priority, COUNT(*) as n FROM reports GROUP BY priority"):
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
    if not os.path.exists(DB_PATH):
        print("⚠️  No se encontró reports.db. Corre primero: python reports.py")
    else:
        init_settings()
        print("🚀 Dashboard corriendo en http://localhost:5000")
        app.run(debug=True, port=5000)
