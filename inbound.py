"""Inbound lead webhook: verify, validate, enrich, score, route, notify.

    WEBHOOK_SECRET=dev-secret python inbound.py          # listens on 127.0.0.1:8080
    python inbound.py --sign sample_payload.json         # print a valid signature for testing

A form tool or n8n posts JSON to /inbound with an X-Signature header holding
the hex HMAC-SHA256 of the raw body. The handler:

1. rejects anything unsigned or wrongly signed (401) or malformed (400),
2. drops duplicate submissions of the same form by the same email (200, no-op),
3. enriches the lead from a local firmographics file (a stand-in for a paid
   enrichment API),
4. scores and routes it,
5. stores it in SQLite (the stand-in CRM) and posts a Slack message for hot
   leads when SLACK_WEBHOOK_URL is set, or prints it when it is not.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
MAX_BODY = 64 * 1024
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
FREE_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "proton.me"}
HOT_SCORE = 70

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    email TEXT NOT NULL,
    form_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    company TEXT, employees INTEGER, industry TEXT, country TEXT,
    score INTEGER NOT NULL, owner TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (email, form_id)
)"""


def sign(body, secret):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body, signature, secret):
    return bool(secret) and hmac.compare_digest(sign(body, secret), signature or "")


def validate(payload):
    """Return a list of problems; empty means the payload is usable."""
    if not isinstance(payload, dict):
        return ["body must be a JSON object"]
    problems = []
    if not EMAIL.match(str(payload.get("email", ""))):
        problems.append("email is missing or invalid")
    if not str(payload.get("form_id", "")).strip():
        problems.append("form_id is required")
    for field in ("first_name", "last_name", "company", "message"):
        if len(str(payload.get(field, ""))) > 500:
            problems.append(f"{field} is too long")
    return problems


def enrich(email, firmographics):
    domain = email.rsplit("@", 1)[1].lower()
    return {"domain": domain, "free_email": domain in FREE_DOMAINS, **firmographics.get(domain, {})}


def score(payload, firm):
    """Simple fit + intent score; see the lead-routing project for the full model."""
    if firm["free_email"]:
        return 0
    employees = firm.get("employees", 0)
    points = 30 if employees >= 1000 else 20 if employees >= 200 else 10 if employees >= 50 else 0
    points += 20 if firm.get("industry") in ("Software", "Fintech") else 0
    points += {"demo_request": 45, "pricing": 25, "contact": 15, "newsletter": 0}.get(payload.get("form_id"), 5)
    title = str(payload.get("title", "")).lower()
    points += 15 if re.search(r"\b(ceo|cro|chief|founder|vp|vice president|head|director)\b", title) else 0
    return min(points, 100)


def route(points, firm):
    if points < 40:
        return "nurture"
    employees = firm.get("employees", 0)
    return "ae_enterprise" if employees >= 1000 else "ae_mid_market" if employees >= 200 else "sdr_queue"


def notify(lead, webhook_url):
    text = (f":fire: Hot lead {lead['email']} ({lead.get('company') or 'unknown company'}), "
            f"score {lead['score']}, routed to {lead['owner']}")
    if not webhook_url:
        print("[slack dry-run]", text, file=sys.stderr)
        return
    req = urllib.request.Request(webhook_url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    # ponytail: one attempt, no retry queue; a failed Slack post is logged and
    # the lead is still stored. Add a retry table if alerts must not be missed.
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except OSError as e:
        print(f"[slack] post failed: {e}", file=sys.stderr)


def handle(body, signature, secret, conn, firmographics, webhook_url=None):
    """Process one request body. Returns (http_status, response_dict)."""
    if not verify(body, signature, secret):
        return 401, {"error": "bad signature"}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "body is not valid JSON"}
    problems = validate(payload)
    if problems:
        return 400, {"error": "invalid payload", "problems": problems}

    email = payload["email"].strip().lower()
    form_id = str(payload["form_id"]).strip()
    if conn.execute("SELECT 1 FROM leads WHERE email = ? AND form_id = ?", (email, form_id)).fetchone():
        return 200, {"status": "duplicate", "email": email}

    firm = enrich(email, firmographics)
    points = score(payload, firm)
    lead = {"email": email, "form_id": form_id, "company": firm.get("company") or payload.get("company"),
            "employees": firm.get("employees"), "industry": firm.get("industry"), "country": firm.get("country"),
            "score": points, "owner": route(points, firm)}
    try:
        with conn:
            conn.execute("INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (email, form_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          lead["company"], lead["employees"], lead["industry"], lead["country"], points,
                          lead["owner"], json.dumps(payload)))
    except sqlite3.IntegrityError:  # lost a race with an identical submission
        return 200, {"status": "duplicate", "email": email}
    if points >= HOT_SCORE:
        notify(lead, webhook_url)
    return 202, {"status": "accepted", **lead}


def open_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(SCHEMA)
    return conn


def serve(host, port, db_path):
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        sys.exit("set WEBHOOK_SECRET first; the server refuses to run unauthenticated")
    conn = open_db(db_path)
    firmographics = json.loads((HERE / "firmographics.json").read_text())
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    db_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/inbound":
                return self.reply(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.reply(413, {"error": "body too large"})
            body = self.rfile.read(length)
            # ponytail: one lock around one SQLite connection serializes every
            # request; fine at form-submission volume, move to a queue and a
            # real database before sustained concurrent traffic.
            with db_lock:
                status, response = handle(body, self.headers.get("X-Signature"), secret, conn,
                                          firmographics, webhook_url)
            self.reply(status, response)

        def reply(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    print(f"listening on http://{host}:{port}/inbound", file=sys.stderr)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="leads.db")
    parser.add_argument("--sign", type=Path, metavar="FILE", help="print the X-Signature for FILE and exit")
    args = parser.parse_args(argv)
    if args.sign:
        secret = os.environ.get("WEBHOOK_SECRET") or sys.exit("set WEBHOOK_SECRET first")
        print(sign(args.sign.read_bytes(), secret))
        return
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
