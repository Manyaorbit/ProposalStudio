# Proposal Studio

A private, single-user proposal builder. Use it as a website from any browser, or run it on one computer.

1. Upload a sample proposal (Word, or a text-based PDF).
2. Tick the sections that stay the same (about us, terms, and so on).
3. Type the customer's name **and location**. GPT researches them on the web, using the location to pick the right organisation, and asks you to confirm the match. It then rewrites every other section, keeping the sample's fonts, colours, headings, bullets and spacing.
4. Review and edit each section, then download a **Word (.docx)** file. That is the only download.

Nothing is stored. When you download, press "Start over", log out, close the tab or stop the app, everything the app held is erased.

---

## Option A: run it as a website (nothing to install on your laptop)

You use it from any browser, for example at `https://proposals.yourcompany-or-name.com`. The app runs on a hosting service; your laptop only needs a browser. Hosting is a small monthly cost (about US$7 on Render's "Starter" plan, which keeps it awake; free plans sleep and are slow to wake).

These steps use **Render** as the example. Any host that runs a Docker container and gives you HTTPS works the same way (Railway, Fly.io, Google Cloud Run, and so on). Check your company's IT policy first: see "Before you put this online" below.

1. **Put the project on GitHub (browser only).** Create a free account and a **private** repository, then drag the unzipped project files into it with "Add file, Upload files". Do **not** upload any `.env` file (the zip doesn't contain one).
2. **Create the service.** On render.com choose New, then Blueprint (it reads `render.yaml`), and pick your repository. It builds the Docker image and starts one instance. If you set it up by hand instead, choose "Web Service", runtime Docker, **exactly 1 instance**, health check path `/healthz`.
3. **Add your OpenAI key.** In the service's Environment settings, set `OPENAI_API_KEY`. Render also created a long random `SETUP_TOKEN` there; you'll use it once.
4. **Create your login in the browser.** Open `https://<your-service>.onrender.com/setup`. Enter the setup token, a username and a password (12+ characters). The page shows three values (`APP_USER`, `APP_PASSWORD_HASH`, `TOTP_SECRET`). Nothing is stored by this page.
5. **Set up the authenticator app on your phone.** In Google Authenticator or Microsoft Authenticator choose "Enter a setup key" and type the `TOTP_SECRET` value.
6. **Save the three values** into the service's Environment settings, **delete `SETUP_TOKEN`**, and let it redeploy. The setup page is now gone (it returns "not found" once a login exists).
7. **Sign in** at your address with username, password and the 6-digit code.

Optional: attach your own domain in the host's settings, then add it to `ALLOWED_HOSTS` (comma-separated, no `https://`). On Render the `onrender.com` address is allowed automatically.

Keep these in mind on a host:
- Run **one instance only**. The working memory lives in that process's RAM; a second instance would not share it.
- Never turn on a "persistent disk" for it; it needs none.
- A restart or redeploy wipes memory too, which is what you want.
- The host's log page will show events like "Login failed", never document text.

### Before you put this online

- **Company policy.** Sample proposals and customer details will pass through a hosting provider and OpenAI. Check that your employer allows that for the documents you use. If your samples hold confidential or personal data of other parties, a private, company-approved hosting account is safer than a personal one.
- **Online means exposed to the internet.** The protections below are strong for a one-person tool, but the first time you host it, consider the optional independent security test (a VAPT) from the checklist you shared.

## Option B: run it on one computer (only if you're allowed to install Python)

```bash
cd proposal-studio
python -m venv .venv
# Windows:  .venv\Scripts\activate        Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
python setup_credentials.py     # username, password, OpenAI API key
python app.py
```

Then open **http://127.0.0.1:5055**. This mode listens on your own computer only and doesn't ask for an authenticator code.

To try the screens without spending any OpenAI credit, start it in demo mode (placeholder text, no AI calls):
`PROPOSAL_AI_MOCK=1 python app.py` (Windows PowerShell: `$env:PROPOSAL_AI_MOCK=1; python app.py`).

### Settings (environment variables)

| Setting | Meaning |
|---|---|
| `APP_ENV` | `production` for the website; leave unset for local use. |
| `ALLOWED_HOSTS` | Production only: your site's address(es), comma-separated. Required. |
| `APP_USER`, `APP_PASSWORD_HASH` | Your login (only a bcrypt hash is stored, never the password). |
| `TOTP_SECRET` | Authenticator-app secret. Required in production. |
| `SETUP_TOKEN` | Temporary: turns on the `/setup` page until a login exists. Delete it afterwards. |
| `OPENAI_API_KEY` | Your OpenAI key. |
| `OPENAI_MODEL` | Model for research and writing (default `gpt-4.1`); must support web search. |
| `PORT` | Set by most hosts automatically. Default 5055. |

---

## How "no memory" works

- The sample, the research and your section choices live in the server's RAM only. No database, no temp files, no uploads folder. Uploads are read straight from the request into memory, and the server is configured so it doesn't spool them to temporary files.
- The browser keeps its working text in page memory only. No localStorage, no cookies except the login cookie.
- Memory is wiped: right after a download (also requested by the browser itself as a second safeguard), on **Start over**, on **Log out**, when the tab is closed or reloaded, after **15 minutes idle**, and when the app stops.
- Opening the app again always starts with an empty workspace.

Honest limits: Python can overwrite the sample file's bytes but cannot guarantee that every copy of text has been scrubbed from RAM or from the operating system's swap file. For a laptop this is a very low risk, but it is not a forensic guarantee. Also, "no memory" applies to this app. See the next section for what OpenAI receives.

## What is sent to OpenAI

- The **customer name and location**, optional website and notes, and the research text.
- For each section being rewritten, the **text of that section from your sample** (so the AI can match its structure, tone and length). Sections you ticked as "stays the same" are never sent.
- Every request sets `store: false`, so OpenAI is asked not to keep the conversation. Check your OpenAI account's data controls too. Do not use sample documents containing information you are not allowed to share with a third-party AI provider.

## Security measures (mapped to the Bachao.AI checklist)

| Checklist area | What this app does |
|---|---|
| Auth and sessions | bcrypt (cost 12) password hash; one user; minimum 12-character password; **second factor: a 6-digit authenticator-app code (RFC 6238), each code usable once**; lockout with exponential back-off from the 5th failed login (30s, 1m, 2m, up to 15m) per address, **plus a site-wide lockout after 10 failures from anywhere**; constant-time checks; random 256-bit session ID in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie that ends when the browser closes; 15-minute idle timeout; a new login ends any older session; CSRF token on every action. |
| Network exposure | Online: answers only to the host names in `ALLOWED_HOSTS` (everything else, including raw IP access, gets a 404); foreign `Origin` / `Sec-Fetch-Site` requests are refused; the app refuses to start in production without 2FA and an allow-list. Local mode listens on `127.0.0.1` only. |
| TLS and headers | Production is HTTPS-only: the host terminates TLS, plain-HTTP requests are redirected, and HSTS is sent (use TLS 1.2+, which hosts enforce). Always sent: strict `Content-Security-Policy` (no inline scripts, no framing), `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, cross-origin isolation headers. |
| Input validation | Server-side checks on everything. Uploads: extension, magic bytes, 12 MB cap, ZIP structure checks (compression-bomb, path-traversal, macro and encryption rejection), PDF page cap. All user text is length-limited and control characters stripped. Everything shown in the page is inserted as text, never as HTML, and Word output is written through an XML library, so typed text cannot inject markup. |
| Secrets | API key, password hash and authenticator secret live in the host's environment settings (or a private `.env` locally), never in code or in the repository. |
| Dependencies | Small list (see `requirements.txt`). The OpenAI API is called directly with `requests`, so there is no extra SDK. After installing, run `pip freeze > requirements.lock` to pin exact versions, and run `pip audit` now and then. |
| Rate limiting | Login throttle (above), a throttle on the setup page, and a cap on AI calls per minute, which also protects your OpenAI bill. |
| Logging | Only events (login ok/failed, wipe, errors by type) printed to the screen. No document text, names or keys are ever logged, and nothing is written to log files. |
| DPDP-style basics | Data minimisation (nothing kept), no personal data stored, a clear statement of what goes to a processor (OpenAI). If your samples contain other people's personal data, check your DPDP obligations. |
| Pre-launch VAPT | Not done. It is optional for a local-only tool, but advisable before you ever put it on a server or share it. |

Not included: sharing with other people. The app has one user. If you hand someone your credentials you must also share the authenticator code, so a better route for a team is separate accounts, which would need more work.

## What it can and can't do

Works well:
- Word samples that use Heading styles (or bold headings) for their sections.
- Keeping fonts, colours, heading levels, bullets, bold lead-ins ("Speed: ...") and spacing, because new paragraphs are copies of the sample's own paragraphs.
- The cover page: lines are adapted one-for-one so layout and alignment are kept.
- Everything you tick as "stays the same" is untouched, byte for byte in content and format.

Limits to know about:
- **Tables, images, text boxes, headers and footers are never rewritten.** They are kept exactly as in the sample. If a table in a rewritten section holds customer-specific content (pricing, say), edit it in Word afterwards. The app warns you about headers/footers that contain text, as they might name the old customer.
- **PDF samples** are rebuilt into an editable Word layout (headings, paragraphs, bullets, fonts, sizes, colours and page size are recovered). Images, tables, columns and exact positioning are not. A Word sample always gives the most faithful result. Scanned PDFs (no selectable text) are not supported.
- The AI is told not to invent facts, prices or references and to use `[square-bracket placeholders]` where it lacks information. **Read every section before sending it to a customer.** The research it was based on is shown so you can check it.
- Document properties such as title/subject are blanked in the output so the old customer's name does not leak through them.

## Tests

```bash
python tests/run_tests.py
```


Runs 90 checks (login lockout, 2FA and replay protection, production host/HTTPS rules, setup page, CSRF, upload rejection, section detection, formatting preservation, memory wiping, PDF import) using demo mode. The test run uses a stand-in for bcrypt only if the real package isn't installed.

## Files

| File | Purpose |
|---|---|
| `app.py` | Web server, routes and security checks |
| `docengine.py` | Reads a Word sample, finds sections, rewrites them while keeping formatting |
| `pdfimport.py` | Rebuilds a PDF sample as an editable Word layout |
| `aiclient.py` | OpenAI calls (web research, section writing) |
| `sessions.py` | RAM-only session and work store, wiping |
| `security.py` | Password check, login throttle, rate limiter, security headers |
| `setup_credentials.py` | One-time login setup for local use (the website uses the `/setup` page instead) |
| `Dockerfile`, `render.yaml` | Hosting files |
| `static/` | The web page (HTML, CSS, JS) |
