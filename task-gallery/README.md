# Private engineering task library

A Python-only Vercel app with server-rendered search, domain filtering, task pages,
engineering review summaries and ZIP downloads. No Google login or database is needed.

Access uses a **shared password**, not a device-bound WebAuthn passkey. Anyone with
it can browse and download the collection. It does not provide individual accounts
or per-person revocation. A signed cookie lasts eight hours; changing either secret
invalidates every existing session. The password and signing secret stay server-side.
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
   - `GALLERY_PASSWORD`: shared password, at least 16 characters; use 24+ random characters.
   - `GALLERY_SESSION_SECRET`: independent secret, at least 32 characters.
5. Deploy. Share the app URL and access password separately.

Generate each secret locally with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Do not set `GALLERY_LOCAL_HTTP` on Vercel. Cookies are Secure in deployment.
Missing/short secrets return a setup page with HTTP 503 and expose no task data.
For Internet-facing deployments, enable Vercel Firewall rate limiting on POST
`/login`; the local failure delay is not a distributed brute-force limit. Use a
long random password. Keep the Git repository private if task content must also
be private in source control. Password protection cannot revoke already downloaded files.

## Local preview

```bash
export GALLERY_PASSWORD='your-shared-password-at-least-16-characters'
export GALLERY_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 task-gallery/api/index.py
```

Open http://localhost:8000. The local entry point binds only to localhost and
allows HTTP cookies. Do not use `python -m http.server` on the bundle: it bypasses
this app's authentication.

## Validation scope

Offline HTTP tests cover protected pages and downloads, wrong/right passwords,
logout, expired/tampered sessions, password rotation, CSRF origin checks, and missing
configuration. Export tests check review/selection gating and ZIP contents.
Actual Vercel deployment is still performed by the owner; no remote build has been run.
