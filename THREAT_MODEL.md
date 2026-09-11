# Threat Model

Odysseus is a **self-hosted AI workspace with privileged local access**. This document states the trust boundary so contributors can reason about security decisions without reading through the full auth and middleware stack.

## Trust Boundary

Odysseus is designed for **trusted users on a private network**, not public exposure. The README describes it as "treat it like an admin console" — that framing is accurate. A logged-in admin can execute shell commands, read and write files, send email, and control model serving. This is intentional. The threat model does not try to prevent admins from doing these things. It does try to prevent:

- Unauthenticated access
- Non-admins reaching admin-only capabilities
- The AI agent acting on instructions injected through untrusted content (web results, emails, fetched pages, memories)
- Internal services (ChromaDB, Ollama, SearXNG, etc.) being reachable from outside the host

## Roles and Capabilities

| Capability | Admin | Non-admin (default) |
|---|---|---|
| Chat with agent | ✓ | ✓ |
| Browser tool | ✓ | ✓ |
| Documents | ✓ | ✓ |
| Research mode | ✓ | ✓ |
| Image generation | ✓ | ✓ |
| Memory management | ✓ | ✓ |
| Shell / Python execution | ✓ | ✗ |
| File read / write | ✓ | ✗ |
| Email send / read | ✓ | ✗ |
| MCP tools | ✓ | ✗ |
| Calendar management | ✓ | ✗ |
| Token / webhook management | ✓ | ✗ |
| Model serving | ✓ | ✗ |
| Vault | ✓ | ✗ |
| Settings | ✓ | ✗ |

Non-admin defaults are in `core/auth.py:DEFAULT_PRIVILEGES`. Tool enforcement is in `src/tool_security.py:NON_ADMIN_BLOCKED_TOOLS`. Any tool whose name starts with `mcp__` is also blocked for non-admins. Admins always get full access regardless of stored privilege values.

## Authentication

- **Sessions:** bcrypt passwords, 7-day session tokens stored atomically in `data/sessions.json` via `core/atomic_io.py`.
- **2FA:** TOTP with 8 single-use backup codes. Verified after password check, before session issuance.
- **Reserved usernames:** `internal-tool`, `api`, `demo`, `system` cannot be registered or renamed into. Defined in `core/auth.py:RESERVED_USERNAMES`.
  - `internal-tool` is security-critical: the auth middleware stamps `request.state.current_user = "internal-tool"` only after verifying a trusted loopback connection and a matching in-process token. `require_admin` re-checks the same loopback+token gate as defence-in-depth (not just the stamp) before granting admin unconditionally. A real account with that name would silently pass every `require_admin` check.
- **Orphan sessions:** `validate_token` re-checks that the user record still exists on every call. A deleted user's cookie is dropped on next request rather than continuing to authenticate.

## Internal Tool Loopback

Agent tool calls reach admin-gated HTTP routes over an in-process HTTP loopback. The gate is defined once in `src/internal_tool_auth.py` — a single source of truth shared by both the auth middleware and the route-level `require_admin` dependency. The mechanism:

1. At app startup, `core/middleware.py` generates a random `INTERNAL_TOOL_TOKEN` via `secrets.token_hex(32)`. It is never persisted and never sent to clients. It can instead be fixed per deployment with the `ODYSSEUS_INTERNAL_TOKEN` environment variable.
2. The auth middleware only stamps `request.state.current_user` when `src/internal_tool_auth.internal_tool_request_ok()` holds: the request carries a matching `X-Odysseus-Internal-Token` header (constant-time `compare_digest`) **and** the peer is a trusted loopback host (`127.0.0.1`/`::1`) with no proxy/tunnel forwarding headers (`x-forwarded-for`, `cf-connecting-ip`, …). Requests forwarded by cloudflared/nginx connect from loopback, so the host check alone would let remote visitors inherit local trust; the forwarded-header check closes that.
3. If the loopback call sets `X-Odysseus-Owner`, the request is attributed to that user only when the user actually exists — owner attribution, not authorization, which is checked separately.
4. `require_admin` re-checks the same loopback+token gate itself rather than trusting the middleware stamp alone (defence-in-depth), so the bypass never depends on middleware ordering. A header from a remote client is rejected even with a correct token.

`GET /api/diagnostics/internal-tool` (admin-only) exposes masked configuration facts — header name, `token_source` (env or ephemeral), loopback restriction — never the token value.

The agent may be running in a non-admin user's session, but tool dispatch first calls `src/tool_security.py:owner_is_admin_or_single_user` to verify the session owner is an admin before issuing any loopback call. Non-admin users cannot invoke admin tools even via the agent.

## Prompt-Injection Hardening

External content that reaches the LLM is treated as untrusted via `src/prompt_security.py`:

- `untrusted_context_message(label, content)` wraps the content in a `user`-role message with a header block instructing the model not to follow instructions inside it. Content goes in as data, not as a system instruction.
- `UNTRUSTED_CONTEXT_POLICY` is a system-prompt preamble that states the same policy at the top of every session where untrusted data may appear.

**Untrusted surfaces that must go through this wrapper:** web search results, fetched URLs, emails (read), saved memories, skill text, notes, and any tool output sourced from outside the server. Injecting untrusted content directly into the system role is a security bug.

## Security Headers

`core/middleware.py:SecurityHeadersMiddleware` sets headers on every response:

- `X-Frame-Options: DENY` + `frame-ancestors 'none'` on all routes except tool-render iframes (which are sandboxed at the HTML level).
- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` everywhere.
- **CSP:** nonce-based `script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net`. `style-src 'unsafe-inline'` is intentionally kept — `static/index.html` ships inline `<style>` blocks and JS modules set `style=""` attributes at runtime. Inline styles do not execute script so the risk is visual-only. Removing this requires templating the HTML files and auditing all JS-set style attributes.

## Known Gaps

1. **No shell/filesystem sandbox — RESOLVED.** The agent `bash` and `read_file`/`write_file` tools run as the app process user with no network egress filtering. Mitigation shipped: the `sandbox_mode` setting (`src/settings.py`, values `off`/`restricted`, default `off`). In `restricted` mode the agent loses arbitrary-subprocess + filesystem-write tools (`bash`, `python`, `write_file`, `edit_file`) for **every** session — admins and single-user owners included — enforced at the single tool-dispatch choke point (`src/tool_security.py:sandbox_restricted_tool` + `src/tool_execution.py`). Read-only investigation (`read_file`/`grep`/`glob`/`ls`) stays enabled, mirroring the plan-mode partition. This is an operator kill-switch for the shell capability, not an OS-level sandbox (no seccomp/landlock/network-namespace); `read_file`/`write_file` additionally remain confined to the workspace roots (`tool_path_extra_roots`) with a sensitive-path deny list. Coverage: `tests/test_sandbox_mode.py`. Full OS sandboxing remains a future upgrade ($#1058).

2. **SSRF via `/api/v1/chat` `base_url` — RESOLVED.** A chat-scoped API token used to be able to supply an arbitrary `base_url` that the server forwarded the LLM request to without checking scheme or address. Now `src/url_security.py:validate_public_http_url` rejects any non-public HTTP(S) target (private/loopback/LAN ranges, and DNS that resolves to them) for token-supplied direct `base_url`s, failing closed on DNS errors. Admin-created model endpoints may still intentionally point at private model providers (e.g. local Ollama). Coverage: `tests/test_api_chat_security.py`.

3. **`src/search/` partial consolidation — RESOLVED.** All `src.search` modules (`core`, `providers`, `analytics`, `cache`, `content`, `query`, `ranking`) now alias `services.search` via `sys.modules` replacement — including `ranking.py`, the last weak re-export shim. There are no independent copies left to drift. Covered by `tests/test_search_module_consolidation.py`.

4. **Token scopes are coarse — RESOLVED.** API tokens could only carry `chat` or resource scopes that never restricted the owning user's *privileges*: a `chat`-scoped token of an admin owner reached the agent's shell. Now token callers get the **intersection** of the owner's stored privileges and the token's scopes (`src/token_scopes.py:effective_privileges_for_token`, applied in `routes/chat_routes.py` and `routes/chat_helpers.py:_enforce_chat_privileges`). Unless the token explicitly carries the new `admin` scope (full owner-equivalent access, profile `admin` → `["admin", "chat"]`), every admin-raisable capability is capped at the non-admin baseline; per-user restrictions (denied models, daily caps) are preserved. Resource scopes (`todos:*`, `documents:*`, …) continue to gate the codex/companion/data APIs. Coverage: `tests/test_token_scope_privileges.py`.
