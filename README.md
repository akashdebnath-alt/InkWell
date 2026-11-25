# Inkwell

Inkwell is a creative-writing editor built with TipTap, Vite, FastAPI, MySQL,
ChromaDB, and Anthropic. The writing assistant offers short suggestions,
selected-text feedback, and brief rewrites; it is not intended to write a whole
story, poem, or chapter.

## Windows setup

### 1. Install MySQL

1. Install MySQL Community Server 8 using the Windows MSI installer.
2. Start the **MySQL80** service from Windows Services (or the MySQL
   Installer).
3. Open MySQL Workbench or a MySQL command prompt and create a database and
   dedicated application user:

   ```sql
   CREATE DATABASE inkwell CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER 'inkwell_user'@'localhost' IDENTIFIED BY 'choose-a-unique-password';
   GRANT ALL PRIVILEGES ON inkwell.* TO 'inkwell_user'@'localhost';
   ```

Keep the database password private. If it contains URL-special characters,
percent-encode them before putting it in `DATABASE_URL` (for example, `@`
becomes `%40`).

### 2. Install Python and Node.js

Install Python 3.10 or later and the current Node.js LTS release. In a new
PowerShell window, verify both are on `PATH`:

```powershell
py --version
node --version
npm --version
```

### 3. Configure and run the backend

Open PowerShell in the repository's `backend` directory:

```powershell
py -3.12 -m venv venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `backend\.env` and set each value:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy connection string for the MySQL database and user created above. |
| `ANTHROPIC_API_KEY` | Anthropic API key. Leave the placeholder if you only want to use documents; AI routes will return a helpful `503` until a key is configured. |
| `JWT_SECRET_KEY` | Signing key for login tokens. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and paste the output here. |
| `FAST_MODEL` | Anthropic model used for short ghost-text suggestions. |
| `SMART_MODEL` | Anthropic model used for assistant-panel requests. |
| `CORS_ORIGINS` | Comma-separated browser origins allowed to call the API. The default is `http://localhost:5173`. |

Do not commit `backend\.env` or paste its values into issues or logs. The
checked-in `.env.example` contains placeholders only.

Start the API from the `backend` directory:

```powershell
uvicorn app.main:app --reload --port 8000
```

The API creates its tables at startup. Interactive API documentation is
available at <http://localhost:8000/docs>.

### 4. Run the frontend

In a second PowerShell window, from the repository root:

```powershell
Set-Location frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Use `localhost` consistently; if you use another
host or port, add that exact origin to `CORS_ORIGINS` and restart the backend.

### 5. Run the tests

From `backend`, with its virtual environment active:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -v
```

The test configuration uses an in-memory SQLite database, not the configured
MySQL database. AI and RAG calls in the end-to-end test are mocked, so the suite
does not need an Anthropic key and does not incur API charges.

## Editor features

- Autosaves documents and provides Word (`.docx`) and PDF downloads. PDF
  generation uses ReportLab: its Python wheels install cleanly on Windows and
  it does not require the external rendering executables that WeasyPrint needs.
- DOCX/PDF exports include headings, bold, italic, underline, lists, and text
  alignment.
- The dark-mode preference is saved in browser local storage.
- `Ctrl+S` saves immediately; `Ctrl+B`, `Ctrl+I`, and `Ctrl+U` format text;
  `Ctrl+Z` and `Ctrl+Y` undo and redo. On macOS, the equivalent `Command` keys
  are supported.
- The status bar shows word and character counts.
- Version history lists timed snapshots. Restoring a snapshot preserves the
  current document as a new version first.
- Reference `.txt` and `.md` files are indexed per document for retrieval.

## API and AI behavior

- Documents and references require a bearer token. A user can only access
  documents they own.
- `POST /reference` accepts a `.txt` or `.md` file up to 1 MB.
- `POST /suggest` streams short ghost text as server-sent events. The editor's
  faded suggestion is accepted with `Tab` or dismissed with `Esc`.
- `POST /assist` supports `synonyms`, `rhymes`, `imagery`, `feedback`, and
  `rewrite_short`. The assistant works on selected text and does not generate
  an entire creative work.
- AI requests are limited to 60 per user per minute. Login and registration
  share a 10-attempt-per-IP-per-minute limit.

## Troubleshooting

- **MySQL connection refused:** Check that the MySQL80 service is running and
  that `DATABASE_URL` uses the correct host, port, database, and user.
- **MySQL access denied:** Re-run the `CREATE USER`/`GRANT` commands with the
  same username and password used in `DATABASE_URL`.
- **Database URL password errors:** URL-encode special characters in the
  password; do not add unescaped `@`, `/`, or `:` characters.
- **PowerShell will not activate the virtual environment:** Run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that
  PowerShell window, then activate it again.
- **`No module named app`:** Run Uvicorn from the `backend` directory using
  `uvicorn app.main:app --reload --port 8000`.
- **Frontend reports a network/CORS error:** Keep both servers running, open
  the frontend at `http://localhost:5173`, and ensure that origin is present in
  `CORS_ORIGINS`.
- **AI features return `503`:** Set a valid `ANTHROPIC_API_KEY` in
  `backend\.env`, then restart the backend.
- **`node` or `npm` is not recognized:** Install Node.js LTS and open a new
  PowerShell window so the updated `PATH` is loaded.
- **PDF fonts differ across machines:** ReportLab uses Arial on Windows and
  falls back to a bundled standard PDF font if system fonts are unavailable.
