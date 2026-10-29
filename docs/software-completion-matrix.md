# NexAlert software completion matrix

Status describes evidence in this checkout. `VERIFIED IN SOFTWARE` means an automated test or a directly inspected implementation path exists; it does not imply physical validation or production security.

| Feature | Status | Evidence | Dependencies / acceptance criteria | Remaining requirements |
|---|---|---|---|---|
| Citizen onboarding and session-backed authorisation | VERIFIED IN SOFTWARE | `backend/app.py`; security and workflow tests | Temporary pseudonymous identities; server-side role checks and session-version checks | Device-loss identity recovery is intentionally unavailable |
| SOS persistence and stable incident ID | VERIFIED IN SOFTWARE | `alerts`, `outbound_queue`; `tests/test_sos_workflow.py` | Commit before HTTP acceptance; retry uses same incident/idempotency key | Browser durability before first successful server response remains device-local |
| Asynchronous simulated delivery worker | PARTIAL | `backend/worker.py`; `SimulatedTransport.process_queue(limit)`; async acceptance test | Run exactly one worker; destination persistence before receipt; retries bounded in DB | Radio adapter queue selection/lease contract is not unified; worker is not yet installed by a Pi installer |
| Command-centre store | PARTIAL | Separate `NEXALERT_CC_DB`; independent SQLite schema and receipts | Dashboard must read only this store; operator lifecycle and audit must be durable there | Several dashboard lifecycle APIs still use the sender database; migration of lifecycle/history is incomplete |
| Fallback incident ownership | VERIFIED IN SOFTWARE | `backend/failover.py`, `backend/app.py`; failover ownership tests | Previously provisioned citizen UUID plus node scoped assertion; conflicts fail closed | Shared node-key compromise permits assertions for provisioned identities; physical key provisioning absent |
| Legacy/unknown ownership | VERIFIED IN SOFTWARE | Additive security migration; fallback tests | Do not infer ownership from name; operator review path | Review UI/process needs operational definition |
| Radio frame encoding | PARTIAL | `backend/radio_protocol.py`; codec tests; max 240 byte framing | V1 JSON payload and CRC32 only; fixed-width node/message IDs | No authenticated compact V2, replay-safe sequence persistence, C++ golden vectors, provisioning package, or firmware codec |
| Radio transport boundary | SIMULATED | `backend/radio_transport.py`; fake-link tests; default simulator | Injection boundary is testable and TX gate defaults closed | No configured driver, verified hardware, or RF transmission |
| Private messages | NOT IMPLEMENTED | Current message rows and API store plaintext | E2EE requires authenticated key association, browser crypto and device key lifecycle | Do not use for confidential information; E2EE must precede any “encrypted” claim |
| Official public announcements | PARTIAL | Operator route, persisted announcement author, local delivery event | Operator only and separate from private/incident records | Per-recipient delivery receipts are not implemented |
| Offline map library and tiles | NOT IMPLEMENTED | Current UI plots coordinates only | Local MBTiles/PMTiles endpoint and bundled map library needed | Pilot region and attribution package are INPUT REQUIRED |
| WebRTC call signalling | PARTIAL | Authenticated Socket.IO signalling tests; browser WebRTC shell | Trusted HTTPS context, participant-scoped calls and stale-session rejection | No trusted phone certificate onboarding or physical two-phone call test; TURN is absent |
| Pi application deployment | PARTIAL | Python requirements, Flask entry point, DB path configuration | Install exact dependencies; configure durable paths and secret; supervise app and one worker | No reviewed Pi OS/AP/captive portal/certificate installer; do not apply network settings from this checkout |
| Local HTTPS/captive portal | SOFTWARE CONFIGURED / DEPLOYMENT INPUT | Loopback TLS reverse-proxy template, explicit certificate/key validation and trusted-proxy checks | Secure cookies and production operator login require HTTPS; local DNS plus CA/device trust required | Operator-only network, DNS, firewall and phone trust onboarding are not provisioned or physically tested |
| Socket revocation | VERIFIED IN SOFTWARE / SINGLE PROCESS | Per-SID protected delivery revalidates current DB role, session version and fixed expiry; deterministic expiry reaper; login/CLI/logout revocation tests | All protected emissions target individually checked SIDs; citizen room selection cannot grant delivery | One web process is supported and entrypoint-locked; no multi-worker or multi-host coordination |
| Load/100-client evidence | NOT IMPLEMENTED | No load test or measured hardware data | Reproducible 100-client software test and resource report | Actual Wi-Fi capacity remains hardware-specific and unverified |
| Firmware integration interface | PARTIAL | `firmware/esp32_sx1262/radio_port.hpp`, config guard | Host codec and driver interface must compile independently | Build is intentionally blocked until exact HAT/board/pin configuration is verified |
| Production safety | PARTIAL | Radio adapter disabled by default; temp DB verification | Explicit secrets and independent data paths | HTTP confidentiality, ownership-key compromise, single-process limit and plaintext messages remain blockers |

## Delivery semantics

- `saved_locally`: the application database transaction committed the incident.
- `queued`: it is in the persistent outbox awaiting a worker attempt.
- `retry`: a failed attempt is retained with an exponential due time; after the configured attempt ceiling the row becomes `failed` and requires operator recovery.
- `received_by_command_centre`: a matching receipt exists only after the separate command-centre database committed the incident.
- Radio TX completion or a link ACK alone never means command-centre receipt. The current simulated path implements the destination receipt; the existing physical adapter remains a non-production test boundary.

The HTTP submit route returns after local persistence. Run `python -m backend.worker` as one separately supervised process. Its advisory file lock prevents a second worker on the same host; this is not distributed coordination and does not make multi-host SQLite safe. The worker uses a bounded batch and critical/high/moderate/routine ordering. Queue capacity is 5,000 pending SOS reports; full queues return HTTP 503 and preserve the citizen's ability to retry with the same incident ID.

## Known software completion blockers

The phase is not complete against the requested definition: private messages remain plaintext; the radio protocol is v1 with CRC but no authentication; there is no C++ golden codec; dashboard reads and lifecycle persistence are not fully separated from the sender; there is no bundled offline map library/tile endpoint, Pi deployment installer, local CA onboarding, or 100-client load test. No physical radio, phone, captive portal, or pilot-region validation was performed.
