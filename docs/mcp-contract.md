# Observed MCP Contract — Google Workspace MCP Server

| Field | Value |
|---|---|
| Status | **Read-only handshake verified** against the original deployment (2026-10-14); the Phase 0 test writes in §6 were not recorded. **Update 2026-10-17:** a bearer-auth Railway deployment, `https://mcp-server-4-production-b6ec.up.railway.app/`, showed the same server name, protocol, and tool contract without `gmail_send_email`. It rejected handshakes without credentials and with a wrong token, and the real writes returned the §4 envelope (`ok: true`, `status: appended` / `draft_created`). See [mcp-server-security.md §6](mcp-server-security.md#6-railway-deployment-with-bearer-auth-verified-2026-10-17) |
| Verified at | 2026-10-14T08:30:18Z; re-verified 2026-10-14T12:01Z (endpoint, tools, and `auth_enforced=false` unchanged); re-verified again with the Phase 5 client (`groww_pulse.delivery.mcp_client`, read-only: handshake, contract compatible, `auth_enforced=false`, security gate failed) |
| Verified by | `scripts/check_mcp.py` (Python `mcp` SDK 1.30.0, Streamable HTTP) |
| Raw output | [`mcp-tools-observed.json`](mcp-tools-observed.json), verbatim `tools/list` result |
| Server source inspected | `saksham20189575/mcp-server-4` @ `203fa75` (2026-09-03) |
| Deployed endpoint | `https://mcp-server-4-production-0fca.up.railway.app/` |

Every claim below is labelled **Observed** (seen in a live response) or **From source** (read in the server's code but not yet seen live).

## 1. Endpoint and transport

| Item | Finding | Basis |
|---|---|---|
| MCP endpoint path | The **root path `/`**, not `/mcp`. Plain `GET /`, `GET /health`, `GET /oauth/start`, and `GET /oauth/callback` are handled as HTTP routes. All other requests (MCP `POST /`) go to the MCP handler | Observed + from source (`src/http/routes.ts`, `src/mcp/server.ts`) |
| Transport | Streamable HTTP | Observed |
| Negotiated protocol version | `2025-11-25` | Observed |
| Server info | `google-workspace-mcp-server` v`1.0.0`; capabilities `tools.listChanged = true` | Observed |
| `GET /` | `200`, `{"service":"google-workspace-mcp-server","status":"running",…,"mcp":"/"}` | Observed. **Not a health signal** |
| `GET /health` | `200`, `{"status":"ok"}`. Static; it does not check Google credentials | Observed + from source. **Not a health signal** |

So `MCP_SERVER_URL=https://mcp-server-4-production-0fca.up.railway.app/` (with the trailing slash).

## 2. Authentication model

| Item | Finding | Basis |
|---|---|---|
| Client → MCP server auth | **None.** `initialize` and `tools/list` succeed with no `Authorization` header. A dummy bearer token is ignored (`check_mcp.py` reports `auth_enforced: false`). The source has no auth middleware | Observed + from source |
| OAuth routes | `GET /oauth/start` is public and redirects to Google consent **without a `state` parameter**. `/oauth/callback` saves whatever tokens it receives | Observed (redirect inspected, not followed) + from source |
| Server → Google auth | OAuth tokens held server-side, either `GOOGLE_TOKENS_JSON` or the file `tokens.json`. Scopes: `gmail.compose` and `documents` | From source |
| Which Google account is authorized | **Unknown.** Only a tool call reveals it, through the resulting draft or append | Not yet verified |
| Whether the server's Google tokens are valid | **Unknown.** A missing or expired token surfaces only as a tool error (`GOOGLE_AUTH_REQUIRED`, `GOOGLE_TOKEN_REFRESH_FAILED`) | Not yet verified |

### Security findings
The full risk assessment and a proposed server-side fix are in **[mcp-server-security.md](mcp-server-security.md)**. In short:
1. **The server is unauthenticated.** Anyone who knows the URL can call `gmail_send_email` (send mail as the authorized account) and `google_docs_append_content` (append to any Doc that account can edit).
2. **The OAuth routes are public and have no `state` check.** The Dockerfile defaults to the file token store. If the deployment keeps that default, anyone who completes the flow replaces the server's Google credentials, and our drafts and appends would land in *their* account.
3. Our client sends `MCP_AUTH_TOKEN` as a bearer header when configured, so it keeps working once auth is added.

## 3. Tools (observed via `tools/list`)

| Tool | Our policy | Required inputs | Optional inputs | Annotations |
|---|---|---|---|---|
| `google_docs_append_content` | **Allowed** | `documentId: string`, `content: string` | `tabId: string`, `ensureLeadingNewline: boolean` (default `false`) | `destructiveHint=false`, `idempotentHint=false` |
| `gmail_create_draft` | **Allowed** | `to: string[]` (email, min 1), `subject: string`, `body: string` | `cc: string[]`, `bcc: string[]`, `bodyType: "text" \| "html"` (default `text`) | `destructiveHint=false`, `idempotentHint=false` |
| `gmail_send_email` | **Blocked**: `ForbiddenToolError` before any network I/O | same as draft | same as draft | `destructiveHint=true` |

All input schemas set `additionalProperties: false`. Unknown fields are rejected.

This confirms that `gmail_send_email` exists on the deployed server, so the code-level allowlist (`groww_pulse.delivery.contract.ALLOWED_TOOLS`) is required.

## 4. Result envelope (from source; to be confirmed by the test writes)

Success (`isError` absent), returned both as `structuredContent` and as JSON text content:
```json
{"ok": true, "action": "google_docs_append_content", "status": "appended",
 "documentId": "…", "charactersAppended": 123, "revisionId": "…"}
{"ok": true, "action": "gmail_create_draft", "status": "draft_created",
 "draftId": "…", "threadId": "…"}
```
Failure (`isError: true`):
```json
{"ok": false, "action": "…", "error": {"code": "DOC_NOT_FOUND", "message": "…", "retryable": false}}
```
Error codes:
- `INVALID_INPUT`
- `AUTH_NOT_CONFIGURED`
- `GOOGLE_AUTH_REQUIRED`
- `GOOGLE_TOKEN_REFRESH_FAILED`
- `INSUFFICIENT_SCOPE`
- `GMAIL_DRAFT_CREATE_FAILED`
- `GMAIL_SEND_FAILED`
- `DOC_NOT_FOUND`
- `DOC_PERMISSION_DENIED`
- `DOC_APPEND_FAILED`
- `GOOGLE_RATE_LIMITED`
- `GOOGLE_API_UNAVAILABLE`
- `PAYLOAD_TOO_LARGE`
- `INTERNAL_ERROR`

Payload limits are 1 MiB by default (`MAX_DOC_APPEND_BYTES`, `MAX_EMAIL_BODY_BYTES`).

## 5. Consequences for our design

- **A5 confirmed:** there is no tool to read a Doc or list drafts. Idempotency must come from our run ledger.
- Both write tools are `idempotentHint=false`. Never retry a tool call after the request may have reached the server; retry only connection-level failures that occur before the request is sent.
- Success is `isError != true` **and** `structuredContent.ok == true` **and** an expected `status` (`appended` / `draft_created`).
- `tools.listChanged = true`: re-run `tools/list` and the contract check before every publish.
- `to` is an array. The `PULSE_EMAIL_TO` comma list is split before sending.

## 6. Remaining verification (needs operator action)

1. Create a **test** Google Doc that the server's Google account can edit, and put its ID in `.env` as `GOOGLE_DOC_ID`.
2. Set `PULSE_EMAIL_TO` to **your own** address. The draft is created in the server's Gmail account, not sent.
3. Run:
   ```bash
   python scripts/check_mcp.py --test-append --test-draft --i-understand-this-writes
   ```
4. Paste the redacted `test_writes` result into this section. Confirm the line appears in the Doc and the draft appears in **the server account's** Drafts folder.
