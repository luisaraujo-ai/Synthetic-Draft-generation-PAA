# Private engineering task library

A Python-only Vercel app with server-rendered search, domain filtering, task pages,
engineering review summaries, registered ZIP downloads and task feedback.
No Google login is needed. PostgreSQL stores deployed download and feedback records;
the local preview uses SQLite.

Access uses a **shared password**, not a device-bound WebAuthn passkey. Anyone with
it can browse and download the collection. It does not provide individual accounts
or per-person revocation. A signed cookie lasts eight hours; changing the password
invalidates every existing session. The password stays server-side; the cookie-signing key is derived internally.
All routes, including direct downloads, pass through the same server handler. Responses
are private/no-store. Task text is HTML-escaped and there are no third-party scripts.

## Prepare the content

From the repository root, after Stage 1 and batch novelty:

```bash
.venv/bin/python export_gallery.py \
  runs/20261023-162428-run \
  runs/20261023-183125-run \
  runs/20261024-102325-parallel-0ff97d
```

Each run's `novelty_selection.json` chooses which current Stage 1-advanced tasks
are exported. No model calls occur. A stale review is skipped; no selected tasks
is an error that preserves the prior catalog. Exported content goes under
`gallery_data/`, not a public/static directory. Commit the refreshed bundle before
redeploying through Git. Only explicit task/review files and inputs are included.
Downloads are limited to 4 MB each to stay below Vercel's function response limit.
The catalog is published as a snapshot; exporting again replaces its list.

## Deploy on Vercel

1. Push this repository to your Git host.
2. Import it in Vercel. Set **Root Directory** to `task-gallery`.
3. Choose **Other** for Framework Preset. Leave Build Command and Output Directory
   at their defaults; there is no frontend compilation step.
4. Add these Environment Variables for Production and Preview:
   - `GALLERY_PASSWORD`: the shared access password. Use a long random password.
   - `GALLERY_ADMIN_PASSWORD`: a different password, shared only with administrators.
   - `GALLERY_DATABASE_URL`: the PostgreSQL connection string, including the database
     provider's TLS options (for example `?sslmode=require`). Use separate databases
     for Production and Preview so preview testing does not affect production records.
   Neon’s automatically injected `DATABASE_URL` is also supported; no duplicate
   variable is required. `GALLERY_DATABASE_URL` takes precedence if both are set.
   The database user must be able to create the `gallery_events` table. It is created
   automatically on first use. An existing `GALLERY_SESSION_SECRET` can be deleted.
5. Deploy. Share the app URL and access password separately.

Optionally generate a password locally with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Do not set `GALLERY_LOCAL_HTTP` on Vercel. Cookies are Secure in deployment.
A missing or empty password returns a setup page with HTTP 503 and expose no task data.
For Internet-facing deployments, enable Vercel Firewall rate limiting on POST
`/login` and `/admin/login`; the local failure delay is not a distributed brute-force limit. Use a
long random password. Keep the Git repository private if task content must also
be private in source control. Password protection cannot revoke already downloaded files.

## Local preview

```bash
export GALLERY_ADMIN_PASSWORD='your-separate-admin-password'
export GALLERY_PASSWORD='your-shared-access-password'
python3 task-gallery/api/index.py
```

Open http://localhost:8000. The local entry point binds only to localhost and
allows HTTP cookies. Do not use `python -m http.server` on the bundle: it bypasses
this app's authentication.

## Download registration and feedback

Clicking Download ZIP opens a name form. Submitting a nonempty name registers the
request before serving the ZIP. The name and latest download date per version appear
under **Downloaded by** on the task page, visible to anyone with gallery access.
Names are self-reported, not verified identities or active-work claims. Repeated
requests are counted separately; the public list groups the same name and version.
A registered download means a ZIP request, not proof the file was saved or opened.
No records are created by merely opening the confirmation page.

Each task has a feedback form with required feasibility (Yes/No/Unsure) and difficulty
(Easy/Moderate/Hard/Unsure), estimated hours to make an easy task hard (required when
Easy is selected), an optional name, a category, and up to 10,000 characters of optional
pasted feedback. Hours accept decimals from 0 through 10,000. Feedback is private to
administrators. Download and feedback records include the task identity, title,
content/bundle version hash, UTC timestamp, and a random browser ID. Existing catalogs
work without re-exporting. Future exports include the ZIP hash explicitly. Records
remain in the database when the catalog is replaced, and stale forms are rejected
rather than silently attaching answers to a changed task.

Open `/admin` (also linked in the footer), enter the separate admin password after
signing into the gallery, and review download counts, named download history and all
feedback. Export CSV or JSON there to collect evidence for pipeline changes. CSV
exports neutralize spreadsheet formulas in submitted text; JSON preserves original
values. Signing out clears both gallery and admin sessions. Neither submissions nor
passwords are written to application logs. The app does not automatically modify
pipeline prompts or generated tasks based on feedback.

PostgreSQL is required for persistent Vercel records. The app deliberately refuses
SQLite on Vercel; local filesystem storage is not a deployed database. For local
preview, `python3 task-gallery/api/index.py` sets a git-ignored SQLite path automatically.
You can override it with `GALLERY_SQLITE_PATH`, or set `GALLERY_DATABASE_URL` to test
PostgreSQL locally after installing `task-gallery/requirements.txt`. The environment
file is an example only; the app does not load `.env` files automatically.

If storage is unconfigured or unavailable, task browsing still works, download
history shows an unavailable message, and download/feedback submissions return HTTP
503 without reporting success. Downloads require successful registration. Add the
new environment variables and redeploy together to enable downloads. Historical
requests before this change cannot be reconstructed by the app.

PostgreSQL access follows the [Psycopg connection and parameter binding API](https://www.psycopg.org/psycopg3/docs/basic/usage.html).

## Validation scope

Offline HTTP tests cover protected pages and downloads, wrong/right passwords,
logout, expired/tampered sessions, password rotation, CSRF origin checks, and missing
configuration. Export tests check review/selection gating and ZIP contents.
Tests also cover required-name downloads, public name escaping, structured feedback
validation, stale versions, persistence across database connections, admin isolation,
CSV/JSON exports, CSRF checks and storage failures. These run against temporary local
SQLite databases. Live PostgreSQL and Vercel deployment have not been exercised.
