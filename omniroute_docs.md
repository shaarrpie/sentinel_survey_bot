# OmniRoute — External Dependency Documentation

**Location:** `C:\Users\tiajungba\.omniroute\` (installed globally via npm)
**Package:** `omniroute` v3.8.49
**Repo:** https://github.com/diegosouzapw/OmniRoute
**Homepage:** https://omniroute.online

---

## 1. What It Is

OmniRoute is a unified AI router / proxy. The Sentinel bot does **not** call LLM providers directly. It calls OmniRoute at `http://localhost:20128/v1`, and OmniRoute routes the request to the actual model provider (OpenAI, Anthropic, Google, etc.) based on the configured routing rules, quotas, and fallback policies.

Think of it as a smart reverse-proxy specifically built for LLM APIs.

---

## 2. How Sentinel Uses It

```
Extension (content.js)
  → backend.py FastAPI server (127.0.0.1:8000/decide)
    → OpenAI Python client (base_url=http://localhost:20128/v1)
      → OmniRoute proxy (port 20128)
        → Actual LLM provider (e.g., OpenAI, Anthropic, etc.)
```

Relevant config in `.env`:
- `BASE_URL=http://localhost:20128/v1`
- `API_KEY=sk-...` (OmniRoute API key, not the provider key)

---

## 3. Directory Structure (Installed Global Package)

```
C:\Users\tiajungba\AppData\Roaming\npm\node_modules\omniroute\
├── package.json              # Project metadata, scripts, dependencies
├── bin/
│   ├── omniroute.mjs         # CLI entry point
│   ├── mcp-server.mjs        # MCP stdio server
│   ├── reset-password.mjs    # Password recovery
│   └── cli/                   # Commander CLI commands
├── src/
│   ├── server/                # Next.js API routes / server-side code
│   │   ├── auth/              # Login guards
│   │   ├── authz/             # Authorization pipeline, policies, CSRF
│   │   ├── cors/              # CORS origin handling
│   │   ├── origin/            # Public origin validation
│   │   └── ws/                # WebSocket live server
│   ├── mitm/                  # Man-in-the-middle proxy engine
│   │   ├── manager.ts         # MITM lifecycle: spawn, certs, DNS, repair
│   │   ├── server.cjs         # MITM server process entry
│   │   ├── cert/              # CA generation, installation, migration
│   │   ├── detection/         # Agent/tool detection (Cursor, Copilot, etc.)
│   │   ├── handlers/          # Per-tool request handlers
│   │   ├── targets/           # Target definitions per tool
│   │   ├── tproxy/            # Transparent proxy (native + JS)
│   │   └── inspector/         # HTTP inspector, SSE merger, pricing
│   ├── sse/                   # Server-Sent Events handlers
│   │   ├── handlers/          # Chat, auto-routing, reasoning routing
│   │   ├── services/          # Auth, cooldown, token refresh, model lookup
│   │   └── utils/             # Backpressure, logging
│   ├── shared/                # Shared utilities, constants, validation
│   │   ├── components/        # React UI components (dashboard)
│   │   ├── constants/         # Providers, models, pricing, feature flags
│   │   ├── schemas/           # Zod validation schemas
│   │   ├── utils/             # API keys, rate limiting, formatting
│   │   └── validation/        # Schema validators
│   ├── lib/                   # Internal libraries (DB, DB adapters, etc.)
│   ├── models/                # Domain models
│   └── types/                 # TypeScript type declarations
├── dist/                      # Compiled/bundled output (gitignored in source)
├── @omniroute/                # Scoped packages
├── open-sse/                  # Workspace package for SSE/openAI compat
├── node_modules/              # Dependencies
├── scripts/                   # Build, dev, test, quality scripts
├── .env.example               # Example configuration
└── logs/                      # Runtime logs (in global install)
```

---

## 4. Key Architectural Files

### 4.1 Entry Point — `bin/omniroute.mjs`
- Fast-paths `--version` before heavy imports
- Loads env files from multiple locations (`DATA_DIR/.env`, `~/.omniroute/.env`, cwd, package root)
- Migrates Electron `server.env` → `.env` for CLI compatibility
- Provisions `STORAGE_ENCRYPTION_KEY` on first run
- Registers tsx ESM alias resolver for global installs
- Delegates to Commander CLI (`bin/cli/program.mjs`)

### 4.2 AuthZ Pipeline — `src/server/authz/pipeline.ts`
- Classifies every request as PUBLIC, CLIENT_API, or MANAGEMENT
- Applies policies: JWT verification, CSRF checks, IP filtering, scope validation
- Stamps peer locality and request IDs
- Routes to policy-specific handlers

### 4.3 MITM Manager — `src/mitm/manager.ts`
- Spawns the MITM child process (`server.cjs`)
- Provisions DNS entries for target agents/tools
- Generates and installs CA certificates
- Detects installed agents (Cursor, Copilot, Claude Code, etc.)
- Handles certificate migration and upstream trust

### 4.4 Chat SSE Handler — `src/sse/handlers/chat.ts`
- Resolves routing model and combo config
- Authenticates requests, refreshes tokens
- Manages account fallback, model lockout, quota exhaustion
- Streams responses via Server-Sent Events
- Handles reasoning effort standardization and context handoffs

---

## 5. Configuration (`.env.example` Highlights)

```env
# Core
PORT=20128
HOST=0.0.0.0
DATA_DIR=~/.omniroute

# Auth
ROUTER_API_KEY=sk-...
STORAGE_ENCRYPTION_KEY=...

# Providers (examples)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...
GOOGLE_API_KEY=...

# Features
DRY_RUN=false
TRACE_ON=true
MITM_ENABLED=false
```

---

## 6. Integration Points with Sentinel

| File | How It Connects |
|---|---|
| `backend.py` | `OpenAI(api_key=API_KEY, base_url=BASE_URL)` — BASE_URL points to `http://localhost:20128/v1` |
| `launch.bat` | Starts `npx omniroute` and waits for `http://localhost:20128/api/monitoring/health` |
| `.env` | `BASE_URL=http://localhost:20128/v1` tells the bot to use OmniRoute instead of a direct provider |

---

## 7. Important Notes

1. **OmniRoute is not part of the Sentinel repo.** It lives outside this workspace and is installed as a global npm package.
2. **The source is TypeScript/Next.js.** It compiles to `dist/` for production.
3. **Do not modify files under `node_modules/`.** Changes will be lost on reinstall.
4. **To update OmniRoute:** `npm install -g omniroute`
5. **Logs and call data** are stored in `C:\Users\tiajungba\.omniroute\logs\` and `call_logs\`.

---

## 8. Quick Reference

| Need | Command |
|---|---|
| Start server | `npx omniroute` |
| CLI help | `omniroute --help` |
| Check status | `curl http://localhost:20128/api/monitoring/health` |
| View logs | `C:\Users\tiajungba\.omniroute\logs\application\app.log` |
| Reset password | `omniroute reset-password` |
