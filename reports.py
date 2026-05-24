#!/usr/bin/env python3
"""
reports.py — Centro de mando de reportes desde Slack.
Jala mensajes del canal #clave10 y menciones, clasifica con Claude,
consulta registros de Linkaform y guarda todo en SQLite.

Uso:
    python reports.py

Requisitos:
    - Python 3.8+
    - Claude Code instalado y autenticado
    - Token de Slack (xoxb-...)
    - Token de Linkaform (JWT)

Configuración:
    Crea un archivo .env en el mismo directorio con:
        SLACK_TOKEN=xoxb-...
        LINKAFORM_TOKEN=tu-jwt-token
"""

import subprocess
import sqlite3
import json
import re
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime


# ─── Cargar .env ──────────────────────────────────────────────────────────────

_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())


# ─── Configuración ────────────────────────────────────────────────────────────

SLACK_TOKEN         = os.getenv("SLACK_TOKEN", "")
LINKAFORM_API_KEY   = os.getenv("LINKAFORM_API_KEY", "")
SLACK_CHANNEL       = "clave10"
MY_USER_ID          = "U07QBM08Z8T"
DB_PATH             = os.path.join(os.path.dirname(__file__), "reports.db")

_linkaform_jwt: str = ""   # cache del JWT en memoria durante la ejecución

# Regex para detectar links o IDs de Linkaform en mensajes
LINKAFORM_PATTERNS = [
    r"https?://app\.linkaform\.com[^\s]+",          # links completos
    r"form_answer[_/]?(\d+)",                        # IDs de respuesta
    r"\b(folio|registro|record|id)[:\s#]+(\d+)\b",  # referencias textuales
]


# ─── Helpers HTTP ─────────────────────────────────────────────────────────────

def http_get(url: str, headers: dict) -> dict:
    """Hace un GET request y retorna JSON."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  ⚠️  HTTP {e.code} en {url}")
        return {}
    except Exception as e:
        print(f"  ⚠️  Error en request: {e}")
        return {}


# ─── SQLite ───────────────────────────────────────────────────────────────────

def init_db():
    """Crea las tablas si no existen."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id                      TEXT PRIMARY KEY,
            slack_ts                TEXT,
            slack_channel           TEXT,
            slack_user              TEXT,
            slack_user_name         TEXT,
            raw_message             TEXT,
            report_type             TEXT,
            priority                TEXT,
            summary                 TEXT,
            key_points              TEXT,
            linkaform_id            TEXT,
            linkaform_data          TEXT,
            status                  TEXT DEFAULT 'nuevo',
            notes                   TEXT DEFAULT '',
            resolution_time_minutes INTEGER,
            resolution_cause        TEXT DEFAULT '',
            deleted                 INTEGER DEFAULT 0,
            created_at              INTEGER,
            updated_at              INTEGER
        )
    """)
    conn.commit()
    # Migración para bases de datos existentes
    for col, typedef in [
        ("resolution_time_minutes", "INTEGER"),
        ("resolution_cause", "TEXT DEFAULT ''"),
        ("deleted", "INTEGER DEFAULT 0"),
        ("completed_at", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {col} {typedef}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # columna ya existe
    # Corregir created_at para que sea el timestamp del hilo de Slack, no de la sincronización
    conn.execute("""
        UPDATE reports
        SET created_at = CAST(CAST(slack_ts AS REAL) AS INTEGER)
        WHERE slack_ts IS NOT NULL AND slack_ts != '' AND CAST(slack_ts AS REAL) > 0
    """)
    conn.commit()
    conn.close()


def report_exists(report_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM reports WHERE id = ?", (report_id,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def save_report(report: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = int(time.time())
    # Usar el timestamp del hilo de Slack como fecha de creación del reporte
    slack_ts = report.get("slack_ts", "")
    created_at = int(float(slack_ts)) if slack_ts else now
    c.execute("""
        INSERT INTO reports (
            id, slack_ts, slack_channel, slack_user, slack_user_name,
            raw_message, report_type, priority, summary, key_points,
            linkaform_id, linkaform_data, status, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        report["id"],
        report["slack_ts"],
        report["slack_channel"],
        report["slack_user"],
        report.get("slack_user_name", ""),
        report["raw_message"],
        report.get("report_type", "desconocido"),
        report.get("priority", "media"),
        report.get("summary", ""),
        json.dumps(report.get("key_points", []), ensure_ascii=False),
        report.get("linkaform_id", ""),
        json.dumps(report.get("linkaform_data", {}), ensure_ascii=False),
        "nuevo",
        "",
        created_at,
        now,
    ))
    conn.commit()
    conn.close()


# ─── Slack ────────────────────────────────────────────────────────────────────

def slack_headers() -> dict:
    return {"Authorization": f"Bearer {SLACK_TOKEN}"}


def get_channel_id(channel_name: str) -> str | None:
    """Busca el ID del canal por nombre."""
    url = "https://slack.com/api/conversations.list?limit=200&types=public_channel,private_channel"
    data = http_get(url, slack_headers())
    for ch in data.get("channels", []):
        if ch["name"] == channel_name:
            return ch["id"]
    return None


def get_channel_messages(channel_id: str, limit: int = 50) -> list:
    """Trae los últimos N mensajes del canal."""
    url = f"https://slack.com/api/conversations.history?channel={channel_id}&limit={limit}"
    data = http_get(url, slack_headers())
    return data.get("messages", [])


def get_mentions(limit: int = 20) -> list:
    """Busca mensajes donde te mencionan."""
    query = urllib.parse.quote(f"<@{MY_USER_ID}>")
    url = f"https://slack.com/api/search.messages?query={query}&count={limit}&sort=timestamp"
    data = http_get(url, slack_headers())
    matches = data.get("messages", {}).get("matches", [])
    return matches


def get_user_name(user_id: str) -> str:
    """Resuelve el nombre de un usuario por su ID."""
    url = f"https://slack.com/api/users.info?user={user_id}"
    data = http_get(url, slack_headers())
    user = data.get("user", {})
    return user.get("real_name") or user.get("name") or user_id


# ─── Linkaform Auth ───────────────────────────────────────────────────────────

def get_linkaform_jwt() -> str:
    """
    Obtiene un JWT fresco usando la API key.
    Lo cachea en memoria para no hacer múltiples requests en la misma ejecución.
    """
    global _linkaform_jwt
    if _linkaform_jwt:
        return _linkaform_jwt

    if not LINKAFORM_API_KEY:
        print("  ⚠️  LINKAFORM_API_KEY no configurada, se omitirán datos de Linkaform.")
        return ""

    url = "https://app.linkaform.com/api/infosync/get_jwt/"
    data = json.dumps({"api_key": LINKAFORM_API_KEY}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            jwt = result.get("jwt") or result.get("jwt_complete") or result.get("token", "")
            if jwt:
                _linkaform_jwt = jwt
                print("  ✅ JWT de Linkaform obtenido")
            else:
                print(f"  ⚠️  Respuesta inesperada del login: {list(result.keys())}")
            return jwt
    except Exception as e:
        print(f"  ⚠️  Error autenticando en Linkaform: {e}")
        return ""


# ─── Linkaform ────────────────────────────────────────────────────────────────

def extract_linkaform_ref(message: str) -> str | None:
    """Extrae el primer link o ID de Linkaform del mensaje."""
    for pattern in LINKAFORM_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def fetch_linkaform_record(ref: str) -> dict:
    """
    Intenta obtener el registro de Linkaform.
    ref puede ser un URL completo o un ID numérico.
    """
    jwt = get_linkaform_jwt()
    if not jwt:
        return {}

    headers = {"Authorization": f"Bearer {jwt}"}

    # Si es URL completo, usarlo directo
    if ref.startswith("http"):
        # Extraer el ID del URL si es posible
        id_match = re.search(r"/(\d+)/?$", ref)
        if id_match:
            record_id = id_match.group(1)
        else:
            return http_get(ref, headers)
    else:
        # Extraer solo dígitos
        digits = re.search(r"\d+", ref)
        if not digits:
            return {}
        record_id = digits.group(0)

    url = f"https://app.linkaform.com/api/infosync/form_answer/{record_id}/"
    return http_get(url, headers)


# ─── IA: Clasificar reporte ───────────────────────────────────────────────────

def classify_report(message: str, linkaform_data: dict) -> dict:
    """
    Llama a Claude Code CLI para clasificar el reporte.
    Retorna tipo, prioridad, resumen y puntos clave.
    """
    linkaform_str = json.dumps(linkaform_data, ensure_ascii=False, indent=2) if linkaform_data else "No disponible"

    prompt = f"""Eres un asistente técnico que ayuda a clasificar reportes de un sistema de software.

MENSAJE DE SLACK:
{message}

DATOS DEL REGISTRO EN LINKAFORM:
{linkaform_str}

Analiza el reporte y responde ÚNICAMENTE con un objeto JSON válido con estas claves:
{{
  "report_type": "bug|incidente|tarea|otro",
  "priority": "alta|media|baja",
  "summary": "Resumen claro en 1-2 oraciones de qué está pasando",
  "key_points": ["punto clave 1", "punto clave 2", "punto clave 3"]
}}

Reglas para prioridad:
- alta: sistema caído, pérdida de datos, bloquea operación crítica
- media: funcionalidad degradada, workaround posible
- baja: mejora, duda, error menor

Responde SOLO con el JSON, sin backticks ni texto adicional."""

    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"  ⚠️  Error en Claude: {result.stderr.strip()}")
        return {
            "report_type": "desconocido",
            "priority": "media",
            "summary": message[:150],
            "key_points": []
        }

    raw = result.stdout.strip()
    raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "report_type": "desconocido",
            "priority": "media",
            "summary": message[:150],
            "key_points": []
        }


# ─── Flujo principal ───────────────────────────────────────────────────────────

def process_message(msg: dict, channel_name: str):
    """Procesa un mensaje individual y lo guarda si es nuevo."""
    ts = msg.get("ts", "")
    user = msg.get("user", "desconocido")
    text = msg.get("text", "").strip()

    if not text or not ts:
        return

    # ID único: canal + timestamp
    report_id = f"{channel_name}_{ts}"

    if report_exists(report_id):
        return  # ya procesado

    print(f"  📨 Nuevo mensaje de {user}: {text[:60]}...")

    # Resolver nombre del usuario
    user_name = get_user_name(user) if user != "desconocido" else "desconocido"

    # Buscar referencia a Linkaform
    lf_ref = extract_linkaform_ref(text)
    lf_data = {}
    lf_id = ""
    if lf_ref:
        print(f"  🔗 Consultando Linkaform: {lf_ref[:60]}...")
        lf_data = fetch_linkaform_record(lf_ref)
        lf_id = lf_ref

    # Clasificar con Claude
    print(f"  🤖 Clasificando con IA...")
    classification = classify_report(text, lf_data)

    report = {
        "id": report_id,
        "slack_ts": ts,
        "slack_channel": channel_name,
        "slack_user": user,
        "slack_user_name": user_name,
        "raw_message": text,
        "linkaform_id": lf_id,
        "linkaform_data": lf_data,
        **classification,
    }

    save_report(report)
    print(f"  ✅ Guardado: [{classification['priority'].upper()}] {classification['summary'][:60]}")


def main():
    # Validar config
    if not SLACK_TOKEN:
        print("❌ Falta SLACK_TOKEN. Agrégalo al archivo .env o como variable de entorno.")
        sys.exit(1)

    print("🚀 Reports — Centro de mando")
    print(f"   Canal: #{SLACK_CHANNEL} | Usuario: {MY_USER_ID}")
    print()
    init_db()
    print("✅ Base de datos lista")

    # Buscar canal
    print(f"🔍 Buscando canal #{SLACK_CHANNEL}...")
    channel_id = get_channel_id(SLACK_CHANNEL)
    if not channel_id:
        print(f"❌ No se encontró el canal #{SLACK_CHANNEL}. Verifica que el bot esté invitado.")
        sys.exit(1)
    print(f"   ID del canal: {channel_id}")

    # Procesar mensajes del canal donde aparezca una mención al usuario
    print(f"\n📥 Jalando mensajes de #{SLACK_CHANNEL}...")
    messages = get_channel_messages(channel_id, limit=5)
    mention_tag = f"<@{MY_USER_ID}>"
    relevant = [m for m in messages if mention_tag in m.get("text", "")]
    print(f"   {len(messages)} mensajes encontrados, {len(relevant)} con mención a @{MY_USER_ID}")
    for msg in relevant:
        process_message(msg, SLACK_CHANNEL)

    # Procesar menciones
    print(f"\n🔔 Buscando menciones de @{MY_USER_ID}...")
    mentions = get_mentions(limit=5)
    print(f"   {len(mentions)} menciones encontradas")
    for mention in mentions:
        # Las menciones tienen estructura ligeramente diferente
        msg = {
            "ts": mention.get("ts", ""),
            "user": mention.get("username", "desconocido"),
            "text": mention.get("text", ""),
        }
        channel_info = mention.get("channel", {})
        ch_name = channel_info.get("name", "dm") if isinstance(channel_info, dict) else "dm"
        process_message(msg, ch_name)

    # Resumen final
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM reports WHERE status = 'nuevo'")
    nuevos = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM reports")
    total = c.fetchone()[0]
    conn.close()

    print(f"\n{'─'*40}")
    print(f"📊 Total en DB: {total} reportes ({nuevos} nuevos)")
    print(f"💾 DB guardada en: {DB_PATH}")
    print(f"\n💡 Abre el dashboard para ver los reportes:")
    print(f"   python server.py")


if __name__ == "__main__":
    main()
