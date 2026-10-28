# NexAlert security baseline — Phase 2G

## Pre-fix findings

The audit was checked against the current Flask routes, SQLite schema, templates, browser scripts, and Socket.IO handlers before implementation.

- Public registration accepted a caller-selected `username` and `role`. A username collision returned the existing user and installed that user's ID in the caller's signed cookie. `role=operator` could be registered publicly.
- `/api/sos` trusted a submitted username (or created a deterministic anonymous username from the idempotency key), so incident ownership was not bound to an authenticated citizen. The full SOS was emitted as `new_alert` to every connected socket.
- `/api/users`, `/api/alerts`, `/api/incidents`, incident detail/history, `/api/messages`, dashboard summary, contact/group operations and sensitive location data had no consistent identity or role checks. Message reads accepted an arbitrary `username` query parameter and included broadcasts.
- Assignment, lifecycle changes and `/api/announcements` writes had no operator authorization. Announcement authors were not recorded.
- Socket `connect` accepted an unauthenticated client, `join` accepted an arbitrary username room, private messages were broadcast by username rooms, and presence/location events were sent globally.
- The Flask session used a hardcoded fallback secret. Session identity was stored in a signed cookie without server-side revocation. There was no CSRF validation.
- Historical alerts do retain `user_id`, but historical users/roles do not have verified credentials. Existing records can remain owned by those database rows; no historical user is treated as an operator or re-authenticated by a display name.

## Intended permission matrix

| Capability | Public | Citizen | Operator |
|---|---:|---:|---:|
| Public pages, static assets, health, minimal transport status | Yes | Yes | Yes |
| Begin pseudonymous citizen onboarding | Yes | — | — |
| Create SOS | No | Own identity | Yes, on behalf of an identified citizen only through normal workflow |
| Read incident details/status | No | Own incidents | All command-centre incidents |
| Update SOS follow-up details | No | Own incidents | No direct ownership override |
| List user records | No | No | Yes, display name and online state only |
| Read/send private messages | No | Own sent/received | No general message access |
| Read public emergency announcements | Yes | Yes | Yes |
| Create official announcement/all-clear | No | No | Yes |
| Assign, dispatch, arrive, resolve | No | No | Yes |
| Read/update personal location | No | Own location only; one-time and optional | No global coordinates |
| Protected Socket.IO events | No | Own status, own messages, public announcements and authorized call peers | Operator incident stream and dashboard updates |

## Implemented security behaviour

### Identities, credentials and sessions

- `/api/register` creates a new random internal user ID and a `citizen-…` username. `display_name`/`full_name` is display text. Requests containing `username`, `user_id`, or `role` are rejected, and duplicate names never select an existing account.
- The browser receives a signed session cookie with an eight-hour lifetime (`HttpOnly`, `SameSite=Lax`, no refresh). Production must set `NEXALERT_SECRET` to at least 32 bytes. Development/test mode may generate an ephemeral secret; development sessions do not survive restart.
- Protected requests and each socket event load the user and current `session_version` from SQLite. Logout and successful operator login increment the version, invalidating previous cookies; logout also disconnects sockets registered by the current process. This is server-checked revocation, though active socket bookkeeping is process-local.
- `/api/operator/login` uses Werkzeug scrypt password hashes, generic failure responses and a five-failure/15-minute account lock. Login is refused in deployment mode unless request HTTPS is active. `NEXALERT_HTTPS=1` enables secure cookies and the documented TLS-proxy login mode; `NEXALERT_TRUST_PROXY=1` trusts one forwarded protocol value and is safe only behind a controlled proxy. Provision operators with `python -m backend.manage_operator --username NAME`; the interactive command creates/updates an operator and invalidates earlier sessions. No default account is shipped.
- All unsafe HTTP methods require the session CSRF token in `X-CSRF-Token`, including registration and login. `/api/csrf` bootstraps the token. The signed failover heartbeat has its own authentication; the internal fallback-forward path uses a process-only random token.
- Historical users are demoted to citizen by a one-time additive security migration. Historical alert `user_id` values remain attached to their original user rows. Those old rows have no verified login credential, so the records remain available in the operator incident view but are not silently restored to a citizen by display name. They do not receive operator access. No identity is reconstructed from names.
- Backup-node incidents record the local citizen's internal ID and `/api/fallback/incidents` returns only rows owned by the authenticated local user. When that incident is reconciled into a separate primary database, the current failover protocol creates a synthetic fallback-node user there; it does not transfer a citizen credential across nodes. The primary incident is therefore operator-visible but not linked to a recoverable citizen account. This is a known cross-node ownership limitation pending a secure identity handoff design.

### Endpoint permission matrix

| Endpoint family | Public | Citizen | Operator |
|---|---|---|---|
| `/`, `/phone`, static assets, `/health`, `/api/csrf`, public announcement read, `/api/status` | Read | Read | Read |
| `/api/register` | Create a server-issued citizen session | Existing session returned | Existing session returned |
| `/api/session`, `/api/logout` | — | Own session | Own session |
| `/api/operator/login` | Operator credential verification only; HTTPS required in deployment | Same | Same |
| `/api/sos`, `/api/fallback/sos` | — | Create under own identity | — |
| `/api/fallback/incidents` | — | Own backup-node incidents only | — |
| `/api/incidents`, detail, follow-up updates | — | Own incidents only; own detail/update | Received command-centre incidents; all authorized details |
| `/api/alerts`, `/api/users`, dashboard summary/failover/action APIs | — | — | Read command-centre data; user list omits coordinates |
| Assignment, lifecycle, resolve, simulated transport, environmental write | — | — | Authorized action; consequential incident/broadcast actions are audited |
| `/api/messages` | — | Own sent/received records; send as self to a citizen | — |
| `/api/location` | — | Update own optional one-time location | — |
| `/api/announcements` read/write | Public announcements only | Public announcements only | Read and create validated official announcements/all-clear |
| `/api/contacts`, `/api/groups` | — | Own contact records; create creator-only groups | — |
| `/api/environmental` read | Latest sensor record | Latest sensor record | Latest sensor record; operator-only write |
| `/api/failover/heartbeat` | Signed service heartbeat only | Same | Same |

Access checks are server-side. Citizens never receive the user directory or a global coordinate list. Incident status and detailed locations are scoped to the owner or operator. Citizens may submit optional coordinates with an SOS, but no continuous location polling is introduced. The operator user list intentionally excludes coordinates.

### Socket.IO audiences

The connect handler requires a valid database-backed session. The server joins each connection to `user:<internal-id>` and joins only operator identities to `operators`; client `join` requests do not add rooms. Full `new_alert` records and dashboard lifecycle/environment updates are sent only to `operators`. The submitting citizen receives a minimal status event in their own room. Private message events go to the sender and recipient internal-ID rooms; call signalling resolves named peers to current authenticated citizen rows and active sockets. Public announcements remain a separate public event. Socket events recheck the session version; logout disconnects this process's sockets for the identity.

### Operator audit trail

Successful assignment, lifecycle transition/resolution, and official announcement writes add an `operator_actions` row with server-authenticated actor ID, action, target incident (when applicable), state/category and server timestamp. Rejected operator lifecycle, assignment, and resolution attempts are recorded with `outcome=rejected` and no free-text request content. Actor IDs in request bodies are ignored. Ordinary routes expose no audit-edit/delete operation. Requests denied to non-operators are rejected before any operator action is attributed. The existing incident history also records SOS delivery and lifecycle state changes. Passwords, cookies, CSRF tokens and private message bodies are not logged.

### Test evidence and operating limits

The regression suite covers unauthorized incident/user reads, cross-citizen message/incident access, registration role/identity spoofing, operator-only announcements and lifecycle changes, socket authentication/audience scoping, CSRF failures, session revocation, actor attribution, and fail-closed missing-secret startup. The regular verification command runs these tests with isolated temporary databases. Passing tests demonstrate those code paths, not an independent penetration test or production certification.

## Known architectural limits

Plain HTTP is supported only for isolated development/local operation. It does not protect passwords, signed session cookies, CSRF tokens, SOS contents, locations, or messages from another network participant able to observe or alter traffic. Access control is not encryption. The application does not implement end-to-end encryption for messages or calls. Browser identity is temporary and cannot be recovered from a display name. Historical rows have preserved ownership IDs, but unverified historical users cannot authenticate to recover those rows. Deployment currently assumes one application process for prompt local socket disconnection; the database session-version check still rejects revoked sessions on subsequent protected requests. These controls do not make the prototype production-secure.
