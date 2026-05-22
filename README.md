# slack-reports

Personal dashboard to manage and track Slack reports using AI classification, SQLite and Flask.

## What it does

Pulls messages from the `#clave10` Slack channel, classifies them with Claude AI, and displays everything in a local web dashboard where you can manage status, add notes, and notify the team back on Slack.

## Stack

- **Python 3.12** — no external dependencies except `flask`
- **SQLite** — local database, single file `reports.db`
- **Claude Code CLI** — AI engine, invoked as `claude -p "prompt"`
- **Slack API** — direct REST with `urllib`
- **Linkaform API** — direct REST with `urllib`, user/password auth → JWT
- **Flask** — local server exposing the REST API and dashboard

## Project structure

```
slack-reports/
├── reports.py        # Main script — fetch, classify and save reports
├── server.py         # Flask server — REST API + dashboard
├── dashboard/
│   └── index.html    # Web dashboard (vanilla HTML/CSS/JS)
├── .env              # Credentials (never commit)
└── reports.db        # SQLite (auto-generated)
```

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/your-user/slack-reports.git
cd slack-reports

# 2. Create and activate virtualenv
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install flask

# 4. Create .env with your credentials
cp .env.example .env
# Edit .env and fill in your tokens
```

## Environment variables

Create a `.env` file in the root directory:

```
SLACK_TOKEN=xoxb-...
LINKAFORM_API_KEY=...
```

## Usage

```bash
# Pull and classify reports from Slack
python reports.py

# Start the dashboard
python server.py
# Open http://localhost:5000
```

## Automate with cron

To pull reports every 5 minutes automatically:

```bash
crontab -e
```

Add this line:

```
*/5 * * * * cd /path/to/slack-reports && /path/to/venv/bin/python reports.py >> sync.log 2>&1
```

## Dashboard features

- **Status management** — nuevo → visto → en revisión → en proceso → resuelto
- **Slack notifications** — send a contextual message to the original thread for each status
- **Notes** — add personal notes to any report
- **Filters** — filter by status in the sidebar
- **Linkaform login** — authenticate with your Linkaform account to enable integrations
- **Auto-refresh** — dashboard refreshes every 60 seconds
- **Manual sync** — trigger a report pull from the dashboard

## Report statuses

| Status | Description |
|---|---|
| `nuevo` | Freshly imported, not reviewed |
| `visto` | Seen, pending review |
| `en_revision` | Actively being reviewed |
| `en_proceso` | Work in progress |
| `resuelto` | Resolved |

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/reports` | List reports. Params: `status`, `priority`, `type`, `limit` |
| GET | `/api/reports/{id}` | Report detail |
| PATCH | `/api/reports/{id}/status` | Update status |
| PATCH | `/api/reports/{id}/notes` | Update notes |
| POST | `/api/reports/{id}/notify` | Send Slack notification |
| GET | `/api/stats` | Counts by status and type |
| POST | `/api/sync` | Trigger report sync |
| POST | `/api/auth/login` | Linkaform login |
| GET | `/api/auth/status` | Auth status |
| POST | `/api/auth/logout` | Logout |
