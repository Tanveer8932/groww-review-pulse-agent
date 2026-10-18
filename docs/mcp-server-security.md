# MCP Server Security Assessment and Proposed Fix

| Field | Value |
|---|---|
| Status | **Fix implemented locally, not deployed** (2026-10-15, see §5). The deployed server is still unauthenticated until the patched build is pushed and deployed |
| Assessed | 2026-10-14 |
| Target | `saksham20189575/mcp-server-4` @ `203fa75` (upstream HEAD re-checked 2026-10-14), deployed at `https://mcp-server-4-production-0fca.up.railway.app/` |
| Owner of fix | Whoever controls that GitHub repo and Railway service. **Ownership is unconfirmed:** it may not be this project's operator |
| Related | [mcp-contract.md](mcp-contract.md) |

## 1. Evidence

| # | Finding | Basis |
|---|---|---|
| E1 | MCP `initialize` + `tools/list` succeed with **no `Authorization` header** | Observed (`scripts/check_mcp.py`, 2026-10-14) |
| E2 | `tools/list` exposes `gmail_send_email` (`destructiveHint=true`) next to the draft and append tools | Observed |
| E3 | No auth middleware anywhere in the request path. `startHttpServer` passes every non-route request directly to `createMcpHandler` | From source (`src/mcp/server.ts`) |
| E4 | `GET /oauth/start` is **publicly reachable**. It answers `302` to Google consent with scopes `gmail.compose` and `documents`, and **no `state` parameter** | Observed (redirect inspected, not followed) + from source (`getAuthUrl`) |
| E5 | `GET /oauth/callback` exchanges any `code` and calls `tokenStore.save()` with no `state` check and no check of *which* Google account authorized | From source (`src/http/routes.ts`) |
| E6 | The Dockerfile defaults to the **file** token store (`GOOGLE_TOKEN_STORE_PATH=/data/tokens.json`). Whether the deployment overrides this with `GOOGLE_TOKENS_JSON` is **unknown** | From source |
| E7 | The `gmail.compose` scope permits **sending**, not only drafting | Google OAuth scope semantics |
| E8 | `/health` is static and does not check Google credentials | From source |

## 2. Risks

| ID | Threat | Precondition | Impact | Severity |
|---|---|---|---|---|
| S1 | **Anyone can send email as the authorized Google account** through `gmail_send_email` (phishing from a trusted address, reputation damage, account suspension) | Knows the URL | Critical | **Critical** |
| S2 | **Anyone can append arbitrary content to any Google Doc the account can edit** (vandalism, injected instructions, misinformation in shared docs) | Knows the URL and a Doc ID | High | **High** |
| S3 | **OAuth credential replacement (login CSRF).** An attacker completes `/oauth/start` with their own Google account; with the file store, the callback overwrites the server's tokens. Our pulses then go to the attacker's Docs and Drafts, or delivery silently "succeeds" in the wrong account | File token store in use (E6); routes public (E4, E5) | High (confidentiality and integrity of internal reports) | **High** (Medium if `GOOGLE_TOKENS_JSON` is used) |
| S4 | Draft spam and quota exhaustion: Gmail and Docs API quota burned, so legitimate runs fail | Knows the URL | Medium | Medium |
| S5 | Misleading health: `/health` = ok while Google auth is broken | — | Low (we never rely on it) | Low |

**Why this matters to us even though our code never sends.** Our allowlist protects against *our* mistakes, not against third parties using the same server. Any pulse we deliver lands in an account whose integrity we cannot currently assume (S3).

## 3. Proposed server-side fix (for the server owner)

Ordered by urgency. Steps 0.x need **no code change**.

### Step 0: immediate containment (configuration only)
- **0.1** If the deployment uses the file token store, set `GOOGLE_TOKENS_JSON` from a locally run `npm run auth` and redeploy. The public callback then cannot change the token the tools use (E6).
- **0.2** If this agent also runs on Railway in the same project, call the server over **Railway private networking** (`http://<service>.railway.internal:<port>/`) and **remove the public domain**. Note: our config allows `http` only for localhost today, so a `*.railway.internal` exception would be needed.
- **0.3** If misuse is suspected, revoke the app's grant at the Google account's *Third-party access* page, re-authorize, and review the Gmail *Sent* folder.

### Step 1: authenticate MCP clients (bearer API keys)
Add middleware in `startHttpServer` **before** `handler.fetch(request)`. It covers every MCP request (`POST`/`GET`/`DELETE` on `/`) and leaves `/health` public:

```ts
// Sketch, not applied and not tested. MCP_API_KEY_SHA256 = comma-separated hex SHA-256 of issued keys.
import { createHash, timingSafeEqual } from "node:crypto";

const keyHashes = (process.env.MCP_API_KEY_SHA256 ?? "").split(",").filter(Boolean)
  .map((h) => Buffer.from(h.trim(), "hex"));

function isAuthorized(header: string | undefined): boolean {
  const m = /^Bearer\s+(.+)$/i.exec(header ?? "");
  if (!m || keyHashes.length === 0) return false;           // fail closed when unconfigured
  const presented = createHash("sha256").update(m[1]).digest();
  return keyHashes.some((h) => h.length === presented.length && timingSafeEqual(h, presented));
}

// in the request handler, after handleHttpRoute(...) returns null:
if (!isAuthorized(req.headers.authorization)) {
  res.writeHead(401, { "WWW-Authenticate": 'Bearer realm="mcp"', "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "unauthorized" }));
  return;
}
```
- Generate at least 32 random bytes per client and store only the hashes. Rotate by adding the new hash, deploying, then removing the old one.
- Also validate the `Origin` header on MCP requests (DNS-rebinding protection recommended by the MCP transport spec) and cap the request body size.

### Step 2: least-privilege tools
- Add `ENABLED_TOOLS` (default: `gmail_create_draft,google_docs_append_content`) and skip `registerTool` for anything else. This removes `gmail_send_email` from `tools/list` entirely, rather than relying on every client's goodwill.
- Optional per-key scopes: map key hash to allowed tools, and reject `tools/call` outside the scope.
- Optional resource allowlists: `ALLOWED_DOC_IDS` and `ALLOWED_RECIPIENT_DOMAINS`, enforced in the services layer.
- Longer term, if sending is ever needed: move to the narrower scopes the tools require, or a separate server.

### Step 3: lock down the OAuth routes
- Production default `ENABLE_OAUTH_ROUTES=false`. Authorize locally (`npm run auth`) and inject `GOOGLE_TOKENS_JSON`.
- If the routes must stay:
  - require the admin bearer key on `/oauth/start`;
  - generate a random, HMAC-signed, short-TTL **`state`**, and reject callbacks whose `state` is missing or invalid;
  - after the token exchange, fetch the account email and **refuse to save** unless it equals `ALLOWED_GOOGLE_ACCOUNT`.

### Step 4: operational hardening
- Per-key rate limiting (for example 30 tool calls per minute).
- An audit log of key ID, tool, outcome, and a hashed Doc ID, never message bodies.
- Make `/health` optionally report `google_auth: ok|missing|expired` without exposing tokens. This app still won't treat it as an MCP health signal.

### Verification the owner should run after the fix
1. Without a key, `initialize` returns `401`. With a valid key, it succeeds.
2. `tools/list` no longer contains `gmail_send_email`.
3. `/oauth/start` returns `401` or `404` without the admin key. A callback with a forged or missing `state` returns `400`, and the token store is unchanged.
4. A wrong-account OAuth completion is rejected.

## 4. Client-side position (this project)

| Control | Status |
|---|---|
| Code-level allowlist; `gmail_send_email` raises `ForbiddenToolError` before any I/O | Done (Phase 0; tested) |
| `MCP_AUTH_TOKEN` sent as `Authorization: Bearer` when configured | Done (`scripts/check_mcp.py` and the Phase 5 client `delivery/mcp_client.py`) |
| `check_mcp.py` reports whether the server **enforces** auth (re-handshakes without the header) | Done (2026-10-14) |
| **Security gate enforced in code (Phase 5).** `publish --confirm` refuses (`SecurityGateError`, no writes) unless `MCP_AUTH_TOKEN` is set **and** handshakes with no credentials and with a wrong token are both rejected with 401/403. There is no override | Done (2026-10-14). Live read-only check that day: `auth_enforced: false`, so live delivery to the deployed server is **blocked** |
| Delivery to production Doc and real recipients | **Blocked by the gate** until Step 1 is deployed. Steps 0.1 and 3 are strongly recommended too, and the authorized account must be confirmed by test writes (mcp-contract.md §6) |
| Alternative if the current owner cannot apply the fix | The operator deploys **their own fork** of mcp-server-4 with Steps 1–3, authorizes their own Google account, and points `MCP_SERVER_URL` at it. No code change is needed in this project |

## 5. Implementation status (2026-10-15)

Steps 1–3 are implemented in a local copy of the server at `../mcp-server-4`, on branch `feat/bearer-auth` (based on `203fa75`). It is **not pushed and not deployed**. Server-side details are in that repo's `docs/authentication.md`.

| Proposal | Implemented as |
|---|---|
| Step 1: bearer keys | `MCP_AUTH_TOKEN_SHA256` (comma-separated SHA-256 hex). Every MCP request to `/` (POST, GET stream, DELETE) is checked with a constant-time comparison **before** the body is read or the MCP handler runs. Missing or invalid tokens get `401` with `WWW-Authenticate: Bearer realm="mcp"`. With `streamable-http`, the server **refuses to start** without hashes. `/health` stays public |
| Step 2: least privilege | `ENABLED_TOOLS`, default `gmail_create_draft,google_docs_append_content`. `gmail_send_email` is no longer registered unless explicitly enabled |
| Step 3: OAuth routes | Off by default (`404`). When `ENABLE_OAUTH_ROUTES=true`: `/oauth/start` needs a separate admin bearer token (`OAUTH_ADMIN_TOKEN_SHA256`); `state` is HMAC-signed (`OAUTH_STATE_SECRET`), expires after 10 minutes and is single-use; the callback verifies the Google ID-token email against `ALLOWED_GOOGLE_ACCOUNT` before saving tokens |

**Verification (local).** Server: 80 vitest tests pass. They cover valid, missing, invalid and non-Bearer tokens over real HTTP and with the SDK client, OAuth start/callback cases, and config fail-closed behaviour. The built server was run with `PORT` set, as on Railway, and checked with `groww-pulse check-mcp`: `healthy: true`, `contract.compatible: true`, `forbidden_tools_present: []`, `auth_enforced: true`, `security_gate_passed: true`. The client gate is unchanged.

**Configuration needed to go live:**

| Where | Variable | Value |
|---|---|---|
| Railway (server) | `MCP_AUTH_TOKEN_SHA256` | Hash from `npm run auth:token`. **Set it before deploying**, otherwise the container exits at startup |
| Railway (server) | `GOOGLE_TOKENS_JSON` | Recommended (output of `npm run tokens:env`), so no file token store is used |
| Railway (server) | `ENABLED_TOOLS`, `ENABLE_OAUTH_ROUTES` | Leave unset (defaults are safe) |
| Groww client `.env` | `MCP_SERVER_URL` | `https://mcp-server-4-production-0fca.up.railway.app/` (or a fork's URL) |
| Groww client `.env` | `MCP_AUTH_TOKEN` | The plaintext token matching the hash |

After deploying, re-run `groww-pulse check-mcp` against the live URL and record the result in `mcp-contract.md`.
