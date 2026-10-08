# FirmSight

FirmSight is an evidence-oriented firmware review workspace. It treats firmware source as untrusted data, never runs imported code, and keeps engineer-approved memory separate from AI suggestions and chat history.

## First milestone workflow

1. Set `FIRMSIGHT_IMPORT_ROOT` to the parent folder containing your firmware projects, then restart the API.
2. Open the dashboard and enter a project directory path inside that configured root.
3. Browse source in the read-only Monaco code viewer.
4. Run a Full Project review to create structured, verifier-backed findings.
5. Inspect evidence, execution path, assumptions, verification, impact, and recommendation; then accept, reject (with reason), or mark intentional.
6. Ask project-aware questions in AI Chat.
7. Save engineer-approved lessons in Engineering Memory.
8. Generate, edit, and validate `firmware.ai.yaml` from the YAML Generator.

AI Review and YAML generation use the configured server-side provider: Investigator proposes structured candidates, then Verifier/Skeptic attempts to disprove them. They never fall back to local pattern matching or a generated template. If the API key or required model is unavailable, FirmSight shows an explicit configuration error. OpenRouter and 9router use the same isolated OpenAI-compatible transport; keys remain server-only and are never returned by the API or UI.

## Run the full stack

Install Python dependencies once:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

This installs the backend runtime dependencies, including `PyYAML>=6.0.3,<7`
for safe Knowledge Base frontmatter parsing and rendering with
`yaml.safe_load`/`yaml.safe_dump`.

Before starting the API, add the server-side provider configuration to `.env`:

```bash
nano .env
```

```dotenv
OPENROUTER_API_KEY='sk-or-v1-...'
# Or for 9router: FIRMSIGHT_9ROUTER_API_KEY='router-secret'
```

FirmSight automatically loads `.env` at backend startup and does not override variables already set by the deployment environment. Keep this file private.

Terminal 1 — API:

```bash
.venv/bin/uvicorn app.main:app --reload
```

Terminal 2 — React dashboard:

```bash
cd apps/web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` requests to FastAPI on port 8000.

For a single-process production-style local run, build the dashboard and start the API; FastAPI serves `apps/web/dist` at `http://127.0.0.1:8000/`.

```bash
cd apps/web
npm run build
cd ../..
.venv/bin/uvicorn app.main:app
```

SQLite data defaults to `./data/firmsight.db`; override this with `FIRMSIGHT_DATABASE`.

## Reset FirmSight local data

Resetting FirmSight removes FirmSight's own persisted records only. It never
deletes your firmware directories or any other unrelated file.

1. Stop the backend (FastAPI) and the frontend dev server (Vite) before touching
   persistence. A running process may hold the database open or rewrite its
   settings during a reset.
2. Determine the database path. Check whether `FIRMSIGHT_DATABASE` is set first;
   otherwise the default is `./data/firmsight.db`.
3. Prefer moving the database to a timestamped backup over deleting it in place,
   so an accidental reset stays recoverable.
4. Restart the backend. A missing database is recreated as an empty database on
   startup, so no explicit init step is required.

For example (Fish):

```fish
set databases_backup ~/firmsight-backups
mkdir -p $databases_backup
set stamp (date +%Y%m%d-%H%M%S)

# Stop FastAPI and Vite first.

# Use FIRMSIGHT_DATABASE if it is set; the default lives under ./data.
if set -q FIRMSIGHT_DATABASE
    set db_path $FIRMSIGHT_DATABASE
else
    set db_path ./data/firmsight.db
end

# Preserve the old database, then let the backend start fresh.
mv $db_path "$databases_backup/firmsight-$stamp.db"

# Restart FastAPI now. The backend recreates ./data/firmsight.db (or the
# FIRMSIGHT_DATABASE path) as an empty database on startup.
```

Notes:

- The reset removes FirmSight records and settings only: projects, source
  indexes, reviews, findings, chat history, YAML generations, Project
  Intelligence, and learning jobs. Your firmware directories and `.env` provider
  credentials are not part of the SQLite database and are never touched.
- The optional Obsidian vault is a Markdown projection that lives outside the
  SQLite database. Resetting SQLite does not delete it. Only manually remove the
  vault files when the configured vault path is dedicated exclusively to
  FirmSight and you also want to clear that projection.
- Always confirm `FIRMSIGHT_DATABASE` before deleting, and never put secrets in
  command output or scripts.

## Local directory import

FirmSight imports a project by having the backend inspect a local directory; the web UI does not upload browser-selected source files. Limit this access to a dedicated parent directory in `.env`, then restart the API:

```dotenv
FIRMSIGHT_IMPORT_ROOT=/home/your-user/firmware-projects
```

The imported directory must be inside that root. FirmSight follows neither directory nor file symlinks, reads only supported files from `lib/`, `src/`, and `include/`, plus root-level `platformio.ini`, `sdkconfig`, and `sdkconfig.defaults`. It never executes the project. Imported content is copied into FirmSight's persisted source index, so future AI analysis uses that indexed snapshot.

## AI provider configuration

The Settings page persists the selected provider identifier, chat-completions endpoint, and model for each FirmSight role. It intentionally cannot save an API key.

Set the secret only in the backend environment, then restart the API:

```bash
# OpenRouter
export OPENROUTER_API_KEY='sk-or-v1-...'
export FIRMSIGHT_AI_PROVIDER='openrouter'

# 9router running locally (start 9router and configure its provider combos first)
export FIRMSIGHT_9ROUTER_API_KEY='router-secret'
export FIRMSIGHT_AI_PROVIDER='9router'
export FIRMSIGHT_AI_ENDPOINT='http://127.0.0.1:20128/v1/chat/completions'

# 9router cloud, if enabled by your account
# export FIRMSIGHT_9ROUTER_API_KEY='router-secret'
# export FIRMSIGHT_AI_PROVIDER='9router'
# export FIRMSIGHT_AI_ENDPOINT='https://9router.com/v1/chat/completions'

# Any other OpenAI-compatible provider
export FIRMSIGHT_AI_API_KEY='provider-secret'
export FIRMSIGHT_AI_PROVIDER='openai-compatible'
```

Use Settings to set the endpoint (always the full `/chat/completions` URL) and role models. The Settings page includes a local 9router preset that fills `9router` and `http://127.0.0.1:20128/v1/chat/completions`; it does not overwrite your model selection or secret. The **Review context per AI request** dropdown controls the bounded source-context budget for each review request. **Structured output mode** defaults to `PROMPT_ONLY` for broad gateway compatibility; `JSON_OBJECT` and `JSON_SCHEMA` send the corresponding OpenAI-compatible `response_format` only when explicitly selected. **Reasoning effort** is an optional provider hint (`UNSPECIFIED`, `LOW`, `MEDIUM`, `HIGH`); reasoning and final content share the configured token limit. **Investigator** and **Verifier output budget** settings retain numeric choices and also offer `PROVIDER_DEFAULT` (**Provider default — no FirmSight output cap**), which omits FirmSight's `max_tokens` field for that structured request. Provider default is not unlimited: the gateway/model still imposes its own limit and may change latency or cost; numeric budgets provide more predictable usage. A reasoning-only or truncated completion is never converted into a finding. The complete prompt includes role instructions, review metadata, and a JSON schema. Smaller context values reduce per-request latency and timeout risk while creating more batches. FirmSight records source-batch inclusion separately from schema-validated AI coverage, so a review with unavailable batches is reported as partial rather than an all-clear result. For example, a router catalog may expose `glm/glm-4-flash`, but model IDs are controlled by the router and must be confirmed there. For GLM Flash on OpenRouter, a pinned model is `z-ai/glm-5.3-flash`; `~z-ai/glm-flash-latest` follows the provider's latest Flash target and can change over time.

During a review, expand **Request diagnostics** to see the request ID, provider/model, batch, lifecycle state, elapsed time, response size, content state, finish reason, and safe failure category. Validation diagnostics report only a category and field paths (never raw completions or source). A completed `0 candidate findings` event means the Investigator returned valid structured JSON and found no qualifying candidate in that batch. A timeout means the provider did not finish the request before FirmSight's 90-second transport limit. A full review is successful only when every planned Investigator batch is schema-validated; otherwise the review remains partial/interrupted and shows its `validated / total` coverage. Chat requests are usually smaller than review batches, so a successful Chat reply does not prove every review request will finish. Token usage is shown only when the selected gateway reports an OpenAI-compatible `usage` object; otherwise FirmSight explicitly shows that usage was not reported. Compare the request ID and timestamp with 9router/FreGateway logs when investigating an upstream wait. Resume is guarded by a source snapshot fingerprint; if the indexed project changes after a partial review, FirmSight rejects the resume and asks you to start a new review so old validated batches cannot be mixed with new source. If the backend process stops while a review is running, the next poll marks the stale job interrupted instead of leaving the UI waiting indefinitely.

The Settings page also exposes **Parallel review requests** (1–3, default 2),
which bounds shared Investigator work and helps control provider overload.

Safe provider policy:

| Gateway capability | FirmSight setting |
| --- | --- |
| Unknown generic provider or 9router combo | `PROMPT_ONLY` + provider-default reasoning |
| Provider documents JSON-object mode | `JSON_OBJECT` |
| Provider documents OpenAI JSON-schema mode | `JSON_SCHEMA` |
| Reasoning-only completion without `message.content` | Choose a route/model that emits final content; do not treat reasoning as a finding |

`max_tokens` is one provider-dependent output limit. It is not a portable reservation for a separate reasoning budget and final-answer budget.

The Settings budgets are stored in the local application database. Existing
provider settings without these fields migrate to the new defaults. The legacy
`FIRMSIGHT_AI_MAX_TOKENS` environment variable remains a fallback for a fresh
database; a persisted Settings value takes precedence.

To smoke-test the configured gateway without printing its response or key (Bash):

```bash
FIRMSIGHT_SMOKE_KEY="${FIRMSIGHT_9ROUTER_API_KEY:-${FIRMSIGHT_AI_API_KEY:-$OPENROUTER_API_KEY}}"
FIRMSIGHT_SMOKE_MODEL="${FIRMSIGHT_SMOKE_MODEL:-glm/glm-4-flash}"
curl -sS -o /tmp/firmsight-ai-response.json -w 'HTTP %{http_code}\n' "$FIRMSIGHT_AI_ENDPOINT" \
  -H "Authorization: Bearer $FIRMSIGHT_SMOKE_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$FIRMSIGHT_SMOKE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK\"}]}"
```

9router exposes an OpenAI-compatible API and commonly listens on `http://localhost:20128/v1`; FirmSight needs the complete completion URL above. See the [9router integration guide](https://github.com/decolua/9router/blob/master/gitbook/content/en/integration/other-tools.md) and [9router documentation](https://docs.9router.com/) for router startup, provider setup, and the current model catalog.

## Safety boundaries

- Import reads source text from a configured local directory; FirmSight does not execute firmware, build scripts, or binaries.
- Paths are normalized and directory traversal is rejected. Individual imports are size-limited.
- Source comments, strings, and documentation are data, not AI instructions.
- Engineering Memory is used for analysis only after an explicit engineer approval action.

## Project Intelligence, Obsidian, and lifetime analysis

FirmSight keeps SQLite as the authoritative store for Project Intelligence. An
Obsidian vault is an optional, portable Markdown projection and retrieval corpus;
editing a Markdown file cannot promote a record to `VERIFIED` or bypass source
revalidation. Configure the server-side vault root with either
`FIRMSIGHT_OBSIDIAN_VAULT_ROOT` or the Knowledge Base section in Settings. An
optional `FIRMSIGHT_OBSIDIAN_ALLOWED_ROOTS` path-separated list restricts where
the root may be created. Sync and search use the saved server setting and never
accept an arbitrary filesystem path from a request.

The generated layout is:

```text
<vault>/Projects/<project-slug>/
  00-Project/ 01-Architecture/ 02-Components/ 03-Reviews/
  04-Knowledge/{Facts,Design-Intent,Architecture,False-Positives,
                Bug-Patterns,Resolution-Patterns,Recurring-Patterns}/
  05-Findings/ 06-Resolutions/ 07-Releases/ 08-Index/ 09-Journal/
```

Knowledge documents contain validated YAML frontmatter with a stable `MEM-*`
ID, project scope, lifecycle status, confidence, provenance, relationships,
and schema version. Writes are atomic. Only Markdown under the known project
directory is scanned; malformed, oversized, symlinked, wrong-project, or
unknown-ID files are reported as quarantined and are never executed or used as
instructions. Current indexed source outranks topology history, intelligence,
Markdown, and lexical/semantic retrieval. `VERIFIED` and `REINFORCED` records
may guide retrieval, `PROVISIONAL` records are tentative, `NEEDS_REVALIDATION`
records are down-ranked with a warning, and `CONFLICTED`, `SUPERSEDED`, and
`DISABLED` records are not treated as current authority.

The static memory-lifetime index recognizes only the supported allocation APIs
(`malloc`, `calloc`, `realloc`, `free`, `new`, `delete`, and the configured
ESP heap variants). It separates releases, unbalanced exits, returned or stored
ownership, unknown callees, and uncertain reallocations. A keyword match does
not prove a leak: a candidate requires a concrete allocation and a plausible
source-backed error/return path with no observed ownership escape. This is
static candidate evidence only; it does not prove runtime heap behavior and does
not replace sanitizers, heap tracing, stress tests, or hardware validation.

Normal vault sync scans and validates existing project Markdown before any
write. A changed file is imported as `ENGINEER_EDITED` notes and its generated
statement/evidence/relationship projection is rebuilt from SQLite. Normal sync
does not overwrite an unimported edit. An explicit service-level
`sync_project(..., regenerate=True)` request may regenerate the projection while
preserving the editable Engineer Notes section; malformed or symlinked files
remain quarantined and are never replaced.

## Verify

```bash
python3 -m pytest -q
cd apps/web && npm run build
```
