"""Proposal Studio: a private, local, single-user proposal builder.

Local:       python app.py       then open http://127.0.0.1:5055
As a website: set APP_ENV=production (see README); the host provides HTTPS.
"""
import datetime
import hmac
import logging
import re
import sys
from functools import wraps

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import aiclient
import docengine
import pdfimport
from config import BASE_DIR, cfg
from security import (SECURITY_HEADERS, LoginThrottle, RateLimiter, new_totp_secret, verify_login,
                      verify_totp)
from sessions import STORE

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proposal")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = cfg.max_upload_bytes
app.config["JSON_SORT_KEYS"] = False
if cfg.trust_proxy:
    # One trusted proxy hop (the host's HTTPS front end): gives the real client IP and the https scheme.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_prefix=0)

THROTTLE = LoginThrottle()
AI_RATE = RateLimiter(limit=60, window=60)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DRAFT_CHARS = 30000


# --------------------------------------------------------------------------- helpers

def err(message, status=400, **extra):
    resp = jsonify({"error": message, **extra})
    resp.status_code = status
    return resp


def clean_line(value, limit):
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def clean_block(value, limit):
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    return value.strip()[:limit]


def safe_filename(company):
    base = re.sub(r"[^A-Za-z0-9 ._-]+", "", company).strip(" .") or "Customer"
    return f"Proposal - {base[:60]}.docx"


def current_session():
    s = STORE.get(request.cookies.get("sid"))
    return s if (s and s.authed) else None


def authed(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        s = current_session()
        if s is None:
            return err("Please log in again. Your session ended and its memory was cleared.", 401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, s.csrf):
                return err("Request rejected.", 403)
        g.s = s
        return fn(*a, **kw)
    return wrapper


# --------------------------------------------------------------------------- request guards

@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.before_request
def guard():
    if request.path == "/healthz":                      # the platform's health check; reveals nothing
        return None
    # Only answer to our own address(es): blocks DNS-rebinding and stray hostnames pointing at the server.
    host = (request.headers.get("Host") or "").lower()
    if cfg.production:
        host = host.split(":")[0]
    if host not in cfg.allowed_hosts:
        return err("Not found.", 404)
    if cfg.https and request.scheme != "https":
        if request.method in ("GET", "HEAD"):
            return redirect("https://" + request.host + request.full_path.rstrip("?"), code=308)
        return err("HTTPS is required.", 400)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and origin not in cfg.allowed_origins:
            return err("Request rejected.", 403)
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return err("Request rejected.", 403)


@app.after_request
def add_headers(resp):
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    if cfg.https:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers.pop("Server", None)
    return resp


@app.errorhandler(413)
def too_large(_):
    return err("That file is too large (limit 12 MB).", 413)


@app.errorhandler(404)
def not_found(_):
    return err("Not found.", 404)


@app.errorhandler(405)
def bad_method(_):
    return err("Not allowed.", 405)


@app.errorhandler(Exception)
def unexpected(e):
    if isinstance(e, HTTPException):
        return err("Request rejected.", e.code or 400)
    log.error("Unhandled error: %s", type(e).__name__)   # type only; never content
    return err("Something went wrong.", 500)


# --------------------------------------------------------------------------- pages and session

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/login")
def login():
    if request.headers.get("X-Requested-With") != "proposal-app":
        return err("Request rejected.", 403)
    if not cfg.password_hash:
        return err("This site hasn't been set up yet.", 503)
    key = request.remote_addr or "local"
    wait = max(THROTTLE.blocked_for(key), THROTTLE.blocked_for("*"))      # this address, or the whole site
    if wait:
        resp = err(f"Too many attempts. Try again in {wait} seconds.", 429)
        resp.headers["Retry-After"] = str(wait)
        return resp
    d = request.get_json(silent=True) or {}
    username = str(d.get("username", ""))[:100]
    password = str(d.get("password", ""))
    code = str(d.get("code", ""))[:12]
    ok = bool(password) and len(password.encode()) <= 72 and verify_login(username, password)
    ok = ok and verify_totp(code)               # second factor is only checked after the password is right
    if not ok:
        THROTTLE.failure(key)
        THROTTLE.failure("*", free_tries=10)    # many different addresses can't get around the limit
        log.warning("Login failed")
        return err("Wrong username, password or code.", 401)
    THROTTLE.success(key)
    THROTTLE.success("*")
    s = STORE.create()
    s.authed = True
    log.info("Login ok")
    resp = jsonify({"csrf": s.csrf})
    resp.set_cookie("sid", s.sid, httponly=True, samesite="Strict", secure=cfg.https, path="/")
    return resp


@app.get("/api/me")
def me():
    s = current_session()
    if s is None:
        return jsonify({"authenticated": False, "totp": bool(cfg.totp_secret)})
    return jsonify({"authenticated": True, "csrf": s.csrf, "ai_ready": cfg.ai_ready,
                    "demo": cfg.mock, "model": cfg.model})


@app.get("/setup")
def setup_page():
    if not cfg.setup_enabled:
        return err("Not found.", 404)
    return send_from_directory(app.static_folder, "setup.html")


@app.post("/api/setup")
def setup_generate():
    """One-time helper so nothing has to be installed: makes your login values to paste into the
    host's settings. Nothing is stored. It switches itself off once APP_PASSWORD_HASH is set."""
    if not cfg.setup_enabled or request.headers.get("X-Requested-With") != "proposal-app":
        return err("Not found.", 404)
    key = "setup:" + (request.remote_addr or "local")
    wait = THROTTLE.blocked_for(key)
    if wait:
        return err(f"Too many attempts. Try again in {wait} seconds.", 429)
    d = request.get_json(silent=True) or {}
    if not hmac.compare_digest(str(d.get("token", "")).encode(), cfg.setup_token.encode()):
        THROTTLE.failure(key, free_tries=3)
        return err("Wrong setup token.", 403)
    username = clean_line(d.get("username"), 60) or "owner"
    password = str(d.get("password", ""))
    if len(password) < 12:
        return err("Use a password of at least 12 characters.")
    if len(password.encode()) > 72:
        return err("That password is too long (72 bytes at most).")
    import bcrypt
    secret = new_totp_secret()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    host = (request.host or "site").split(":")[0]
    uri = f"otpauth://totp/Proposal%20Studio:{username}?secret={secret}&issuer=Proposal%20Studio"
    log.info("Setup values generated")
    return jsonify({"APP_USER": username, "APP_PASSWORD_HASH": hashed, "TOTP_SECRET": secret,
                    "uri": uri, "host": host})


@app.post("/api/logout")
@authed
def logout():
    STORE.destroy(g.s.sid)
    log.info("Logout; memory wiped")
    resp = jsonify({"ok": True})
    resp.delete_cookie("sid", path="/")
    return resp


@app.post("/api/wipe")
def wipe():
    """Clears the work in memory. Also used by the browser when the tab is closed."""
    s = current_session()
    if s is None:
        return jsonify({"ok": True})
    token = request.headers.get("X-CSRF-Token", "")
    if not token:
        token = str((request.get_json(silent=True) or {}).get("csrf", ""))
    if not hmac.compare_digest(token, s.csrf):
        return err("Request rejected.", 403)
    STORE.wipe_work(s.sid)
    log.info("Memory wiped")
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- the workflow

@app.post("/api/upload")
@authed
def upload():
    ext = request.headers.get("X-File-Ext", "").lower().strip(".")
    if ext not in ("docx", "pdf"):
        return err("Please upload a Word (.docx) or PDF file.")
    data = request.get_data(cache=False)              # read in memory; never touches the disk
    if not data:
        return err("That file is empty.")
    notes = []
    try:
        if ext == "docx":
            docengine.validate_docx_bytes(data)
        else:
            data, notes = pdfimport.pdf_to_docx(data)
        parsed, metas, more = docengine.analyze(data)
    except docengine.DocError as e:
        return err(str(e))
    except Exception as e:
        log.warning("Could not read upload: %s", type(e).__name__)
        return err("That file couldn't be read. Try re-saving it and uploading again.")
    notes += more
    if not metas:
        return err("Nothing readable was found in that document.")
    with g.s.lock:
        g.s.work.clear()
        g.s.work.sample = bytearray(data)
        g.s.work.kind = ext
        g.s.work.section_ids = [m["id"] for m in metas]
    return jsonify({"sections": metas, "notes": notes, "kind": ext})


@app.post("/api/research")
@authed
def research():
    work = g.s.work
    if work.sample is None:
        return err("Upload a sample first.")
    if not cfg.ai_ready:
        return err("No OpenAI API key is set. Add OPENAI_API_KEY in the settings and restart.")
    d = request.get_json(silent=True) or {}
    company = clean_line(d.get("company"), 120)
    if len(company) < 2:
        return err("Enter the customer's name.")
    location = clean_line(d.get("location"), 120)
    if len(location) < 2:
        return err("Enter the organisation's location (city and country, for example).")
    website = clean_line(d.get("website"), 200)
    if website and not re.match(r"^https?://[^\s]+$", website):
        return err("The website should start with http:// or https://")
    notes = clean_block(d.get("notes"), 2000)
    fixed = d.get("fixed")
    if not isinstance(fixed, list):
        return err("Choose which sections stay the same.")
    fixed = {str(x) for x in fixed} & set(work.section_ids)
    variable = [sid for sid in work.section_ids if sid not in fixed]
    if not variable:
        return err("Every section is set to stay the same, so there is nothing to write.")
    if not AI_RATE.allow():
        return err("Slow down a little and try again in a minute.", 429)
    try:
        profile, sources = aiclient.research(company, location, website, notes)
    except aiclient.AIError as e:
        return err(str(e), 502)
    with g.s.lock:
        work.fixed, work.company, work.location, work.notes = fixed, company, location, notes
        work.profile, work.sources = profile, sources
    parsed = docengine.load(bytes(work.sample))
    titles = {s.id: (s.title, s.mode) for s in parsed.sections}
    return jsonify({
        "profile": profile, "sources": sources,
        "variable": [{"id": sid, "title": titles[sid][0], "mode": titles[sid][1]} for sid in variable],
    })


@app.post("/api/section")
@authed
def write_one():
    work = g.s.work
    if work.sample is None or not work.company:
        return err("Start again from the upload step.")
    d = request.get_json(silent=True) or {}
    sid = str(d.get("id", ""))
    if sid not in work.section_ids or sid in work.fixed:
        return err("That section isn't set to be rewritten.")
    extra = clean_block(d.get("extra"), 500)
    if not AI_RATE.allow():
        return err("Slow down a little and try again in a minute.", 429)
    try:
        title, mode, markup = docengine.section_markup(bytes(work.sample), sid)
        text = aiclient.write_section(work.company, work.location, work.profile, work.notes,
                                      datetime.date.today().strftime("%d %B %Y"),
                                      title, mode, markup, extra)
    except (docengine.DocError, aiclient.AIError) as e:
        return err(str(e), 502)
    return jsonify({"id": sid, "text": text})


@app.post("/api/download")
@authed
def download():
    work = g.s.work
    if work.sample is None or not work.company:
        return err("Start again from the upload step.")
    d = request.get_json(silent=True) or {}
    raw = d.get("drafts")
    if not isinstance(raw, dict):
        return err("Nothing to build yet.")
    variable = [sid for sid in work.section_ids if sid not in work.fixed]
    drafts = {}
    for sid in variable:
        text = clean_block(raw.get(sid), MAX_DRAFT_CHARS)
        if not text:
            return err("One of the sections is empty. Write it or regenerate it first.")
        drafts[sid] = text
    try:
        out = docengine.build(bytes(work.sample), drafts)
    except docengine.DocError as e:
        return err(str(e))
    except Exception as e:
        log.error("Build failed: %s", type(e).__name__)
        return err("The Word file couldn't be built.", 500)
    name = safe_filename(work.company)
    # A plain Response (not send_file): send_file uses pass-through mode, in which the WSGI server never
    # calls the close hook, so the wipe below would silently never run.
    resp = Response(out, mimetype=DOCX_MIME)
    resp.headers.set("Content-Disposition", "attachment", filename=name)
    sid = g.s.sid
    resp.call_on_close(lambda: (STORE.wipe_work(sid), log.info("Downloaded; memory wiped")))
    return resp


# --------------------------------------------------------------------------- start-up

def main():
    if not cfg.password_hash and not cfg.setup_enabled:
        print("\nNo login is set up yet.\n"
              "  On your own computer:  python setup_credentials.py\n"
              "  As a website: set SETUP_TOKEN (24+ random characters) and open /setup (see README).\n")
        sys.exit(1)
    if cfg.password_hash and (not cfg.user):
        sys.exit("APP_USER is missing.")
    if cfg.production:
        problems = []
        if cfg.password_hash and not cfg.totp_secret:
            problems.append("TOTP_SECRET is required in production (two-factor login).")
        if not cfg.extra_hosts:
            problems.append("ALLOWED_HOSTS is required in production (your site's address, e.g. proposals.example.com).")
        if problems:
            sys.exit("\n".join(["Cannot start:"] + problems))
    STORE.start_sweeper()
    if cfg.setup_enabled:
        log.warning("SETUP MODE: open /setup, create your login, copy the values into the settings, "
                    "then delete SETUP_TOKEN.")
    where = f"http://127.0.0.1:{cfg.port}  (this computer only)" if not cfg.production \
        else f"port {cfg.port}  (production, HTTPS handled by the host)"
    print(f"\nProposal Studio is running at {where}\nStopping the app erases everything it held in memory.\n")
    try:
        from waitress import serve
        # waitress normally spools bigger requests/responses to temp files on disk. The overflow limits
        # are set above the largest possible upload/download so everything stays in memory.
        serve(app, host=cfg.host, port=cfg.port, threads=6, ident="",
              max_request_body_size=cfg.max_upload_bytes + 4096,
              inbuf_overflow=cfg.max_upload_bytes + 65536,
              outbuf_overflow=64 * 1024 * 1024)
    except ImportError:
        app.run(host=cfg.host, port=cfg.port, debug=False, threaded=True)
    finally:
        STORE.wipe_all()


if __name__ == "__main__":
    main()
