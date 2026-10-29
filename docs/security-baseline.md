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
- Protected requests load the user and current `session_version` from SQLite. Socket push delivery independently checks every SID against the database role, version and bound expiry before sending; an expiry reaper disconnects expired sockets. Logout and successful operator login increment the version, invalidating previous cookies. Single-process operation is enforced; multi-worker socket state is unsupported.
- `/api/operator/login` uses Werkzeug scrypt hashes, generic failure responses and persistent per-source throttling, without locking an account due to unauthenticated failures. Production login requires the explicitly configured HTTPS reverse proxy. Operator provisioning/rotation is local CLI-only; the command refuses citizen-to-operator conversion and invalidates previous sessions.
- All unsafe HTTP methods require the session CSRF token in `X-CSRF-Token`, including registration and login. `/api/csrf` bootstraps the token. The signed failover heartbeat has its own authentication; the internal fallback-forward path uses a process-only random token.
- Historical users are demoted to citizen by a one-time additive security migration. Historical alert `user_id` values remain attached to their original user rows. Those old rows have no verified login credential, so the records remain available in the operator incident view but are not silently restored to a citizen by display name. They do not receive operator access. No identity is reconstructed from names.
- Each user has a stable random `identity_id` UUID separate from the local SQLite row ID and display name. The host simulation provisions identities into the fallback store while the primary is available. A real two-node deployment still needs an authenticated provisioning transfer before fallback activation; identities not already in the fallback allowlist are accepted as unclaimed.
- A fallback node signs an ownership assertion bound to the incident ID, stable identity UUID, fallback issuer/source node, and configured primary audience. The primary validates the assertion with the per-backup `NEXALERT_BACKUP_OWNERSHIP_KEY`, resolves the UUID against its own citizen table, and commits the owner plus assertion digest before its normal durable receipt. Retries are idempotent; conflicting owners return a conflict and never replace a verified owner. The key must be unique to a backup link and installed on that backup and the primary. A compromised backup holding that key can forge ownership assertions for any known primary citizen UUID; this design authenticates the trusted node's assertion, not the citizen's real-world identity. Do not reuse the Flask session key or one fleet-wide key.
- Citizens who first join after fallback activation and have no pre-provisioned identity are not cryptographically verified. Their reports remain unclaimed but operator-visible after reconciliation. Legacy records without a verified assertion remain `legacy_unverified` or `unclaimed`; names and old local integer IDs are not used to claim them.
- Each incident additionally has a random 256-bit browser recovery capability; only a SHA-256 commitment is stored server-side. Holding the capability grants scoped access to that incident independent of browser-session validity. A new identity without the capability cannot read or retry it; the old record is not reassigned. Local browser storage loss has no self-service recovery and requires operator review. The capability proves possession only and is exposed to interception if sent over plain HTTP.

### Endpoint permission matrix

| Endpoint family | Public | Citizen | Operator |
|---|---|---|---|
| `/`, `/phone`, static assets, `/health`, `/api/csrf`, public announcement read, `/api/status` | Read | Read | Read |
| `/api/register` | Create a server-issued citizen session | Existing session returned | Existing session returned |
| `/api/session`, `/api/logout` | — | Own session | Own session |
| `/api/operator/login` | Operator credential verification only; HTTPS required in deployment | Same | Same |
| `/api/sos`, `/api/fallback/sos` | Create an unclaimed incident only with a fresh per-incident capability; no client-selected owner | Create with server session ownership; fallback ownership is trusted only with provisioned identity evidence | — |
| `/api/fallback/incidents` | — | Own backup-node incidents only | — |
| `/api/incidents`, detail, follow-up updates | — | Own session incidents; capability holder can access/update only the matching incident | Received command-centre incidents; all authorized details |
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

The connect handler requires a valid database-backed session. The server joins each connection to its internal user room and joins only operator identities to the operator room; client `join` requests do not add rooms. Sensitive delivery does not rely on room broadcast: it sends by SID only after current-session checks. Announcements are delivered to valid citizen sockets by the same path. Logout disconnects current-process sockets, and database checks still revoke on the next push. A five-second reaper enforces expiry. Only one web process is supported.

### Operator audit trail

Successful assignment, lifecycle transition/resolution, and official announcement writes add an `operator_actions` row with server-authenticated actor ID, action, target incident (when applicable), state/category and server timestamp. Rejected operator lifecycle, assignment, and resolution attempts are recorded with `outcome=rejected` and no free-text request content. Actor IDs in request bodies are ignored. Ordinary routes expose no audit-edit/delete operation. Requests denied to non-operators are rejected before any operator action is attributed. The existing incident history also records SOS delivery and lifecycle state changes. Passwords, cookies, CSRF tokens and private message bodies are not logged.

### Test evidence and operating limits

The regression suite covers unauthorized incident/user reads, cross-citizen message/incident access, registration role/identity spoofing, operator-only announcements and lifecycle changes, socket authentication/audience scoping, CSRF failures, session revocation, actor attribution, and fail-closed missing-secret startup. The regular verification command runs these tests with isolated temporary databases. Passing tests demonstrate those code paths, not an independent penetration test or production certification.

## Known architectural limits

Plain HTTP is supported only for isolated development/local operation. It does not protect passwords, signed session cookies, CSRF tokens, SOS contents, locations, or messages from another network participant able to observe or alter traffic. Access control is not encryption. The application does not implement end-to-end encryption for messages or calls, and SQLite is not encrypted at rest. Temporary browser identities have no self-service recovery. Ownership assertions trust a configured backup node and are not proof of real-world identity. Multi-worker socket revocation is unsupported. These controls do not make the prototype production-secure.

## Phase 2H current corrections

This section supersedes the older lockout, `NEXALERT_HTTPS`, `NEXALERT_TRUST_PROXY`, and process-local delivery descriptions above.

- Protected socket delivery revalidates each SID against the current database role, session version, and the original fixed expiry before sending. A five-second reaper disconnects expired sockets without waiting for a later client event. Re-login, CLI credential rotation, role demotion and logout revoke subsequent private/operator pushes. Announcements are individually sent to current citizen sockets. The system supports one web process and enforces that process lock; no multi-worker coordination exists.
- Every new SOS uses a browser-generated 256-bit incident recovery capability. The database stores only its SHA-256 commitment; the browser stores the bearer in local storage. A matching capability permits access/update to only that incident and survives browser-session expiry. It proves possession of that incident key, not the user's real-world identity. A different identity cannot claim an existing incident. Loss of browser storage has no self-service recovery; operator review is required. A bearer sent over plain HTTP can be intercepted on the local network.
- Fallback reconciliation carries the commitment and a node HMAC bound to incident ID, stable citizen UUID, issuer/source, and primary audience. Production requires a separate >=32-byte node key and fails closed without it. HMAC proves that the configured node produced the assertion, not that the citizen was verified in the real world. A compromised fallback can claim a pre-provisioned UUID. Future radio transport would need 64 raw bytes for the capability commitment and SHA-256 HMAC, before identifiers/framing; no v2 format is frozen.
- Request handling applies 64 KiB global and smaller endpoint limits, validates JSON object shape and SOS field sizes/enums, and returns controlled 400/413 responses. Persistent per-address and per-identity fixed-window limits cover onboarding, SOS/fallback, operator login and other high-cost writes. Login failures use generic responses and per-source throttling, without account-only lockout. Rate tables are local to the supported single-process SQLite topology.
- Production Flask binds loopback and requires strong `NEXALERT_SECRET`, valid TLS certificate/key paths, and a literal loopback trusted proxy. Only forwarded headers from the configured proxy address are honored; `NEXALERT_TRUST_PROXY` is rejected. The Nginx example listens on loopback. Phone trust, a protected operator network, DNS and firewall behavior require deployment-specific provisioning and have not been validated on physical devices. Secure cookies require HTTPS for session-backed features.
- Contact synchronization no longer looks up submitted phone numbers against registered accounts. Existing contacts remain stored, with network-match state returned as false; no historical ownership is inferred from phone or display name.

Tests added for expiry/revocation/public announcement scoping, independent report recovery, malformed and oversized input, and deterministic throttling do not establish production security or physical network isolation.
# Phase 2H transport and process note

The SOS route now commits the report and bounded outbox entry before returning; transport execution belongs to the separate `python -m backend.worker` process. One web process and one worker are the only supported SQLite deployment topology. Both use host-local lock files. Those locks do not coordinate across hosts, and Socket.IO session/event state is not shared across web processes. The default simulated transport remains non-radio. See `docs/software-completion-matrix.md` for remaining transport, protocol, message encryption, map, and deployment limitations.
