# NexAlert failover and emergency fallback design

## Implementation status

The host-side state machine, deterministic simulator, fallback SOS API, independent SQLite outbox, dashboard status fields, and reconciliation flow are implemented. This is software simulation only. It does not establish ESP32-S3 failover, local Wi-Fi service, radio connectivity, or the DPR's physical under-90-second result.

The fallback SQLite file is a host simulation of portable local storage. It is separate from both the primary sender database and command-centre receiver database. Tests configure it inside temporary directories.

## Responsibilities

- The Raspberry Pi 5 remains the primary application and command-centre host.
- A future ESP32-S3 backup is expected to monitor authenticated Pi heartbeats and, after a suitably fenced failure decision, serve a local emergency page and save SOS records. It does not take over the full dashboard in this phase.
- The host simulation models both endpoints in one process. Its simulated `primary_running=False` flag is explicit power-off evidence used solely to test promotion. A missed heartbeat on its own is ambiguous and produces `DEGRADED`.
- Radio transport state, node role, failover state, incident delivery state, and responder lifecycle are separate concerns. A simulated transport receipt is not a physical radio acknowledgement.

## State machine

| Current state | Allowed next states | Meaning |
|---|---|---|
| `PRIMARY_ACTIVE` | `BACKUP_STANDBY`, `DEGRADED` | Pi is primary; standby monitor may start. |
| `BACKUP_STANDBY` | `FALLBACK_ACTIVATING`, `DEGRADED` | Backup is monitoring, without master duties. |
| `FALLBACK_ACTIVATING` | `FALLBACK_ACTIVE`, `DEGRADED` | Starting and checking only the local emergency service. |
| `FALLBACK_ACTIVE` | `PRIMARY_RECOVERING`, `DEGRADED` | Local SOS service is available; reports remain on backup. |
| `PRIMARY_RECOVERING` | `SYNCING_INCIDENTS`, `FALLBACK_ACTIVE`, `DEGRADED` | Fresh primary identity observed; primary readiness is checked. |
| `SYNCING_INCIDENTS` | `BACKUP_STANDBY`, `DEGRADED` | Pending backup reports are reconciled. |
| `DEGRADED` | `PRIMARY_RECOVERING`, `BACKUP_STANDBY` | Ambiguous, stale, or failed recovery; no unsafe promotion. |

Illegal transitions raise `FailoverError`. A backup restart invalidates process-relative heartbeat freshness. Standby, activation, recovery, or sync states restart as `DEGRADED` until a fresh heartbeat is observed. A previously active fallback page remains available after restart.

## Heartbeat and failure detection

Version 1 heartbeat fields are `protocol_version`, `node_id`, `master_id`, monotonic `sequence`, wall-clock `timestamp`, and `auth_tag`. The tag is HMAC-SHA256 over a canonical JSON array of the first five fields. This is an authentication extension point; the shared key used by the host simulator is not a production key-management solution. The receiver rejects malformed data, wrong identity/version, timestamps outside the stale/future window, bad tags, and repeated or out-of-order sequence values.

Default simulation tuning is a 5-second cadence, 25-second failure timeout, and 2-second fallback activation target. These values are configurable in `FailoverController`; cadence and timeout are distinct. The injected monotonic and wall clocks make boundaries deterministic in tests. The simulated power-off-to-ready interval is 27 seconds, below the 90-second software target. It is a calculated simulation result, not a physical measurement.

Missing heartbeats can mean power loss, radio/link loss, congestion, or partition. Only the simulator's explicit primary power-off flag permits simulated promotion. A real node must not infer power-off from a timeout alone without a safe fencing/arbitration design.

## Failover, local acceptance, and recovery

1. The backup checks authenticated, fresh Pi heartbeats.
2. In the simulator, confirmed Pi shutdown plus heartbeat timeout transitions through activation. An alive-but-partitioned Pi instead leaves the backup `DEGRADED`.
3. The fallback endpoint accepts only while the local emergency service is marked ready. It validates the report and commits the incident and pending outbox record to the separate fallback SQLite database before returning `accepted_locally`.
4. The response includes a local receipt and explicitly has no command-centre receipt. Incident ID is stable and duplicates from the same source return the original local record.
5. A fresh authenticated Pi heartbeat moves the node to recovery. The primary must also pass its readiness check before `SYNCING_INCIDENTS`.
6. Pending reports are submitted to the existing SOS persistence/transport flow using the same incident and idempotency IDs. The primary writes a separate persistent command-centre incident and receipt before fallback storage marks the outbox row received.
7. Failed transfer, disconnection, or lost acknowledgement retains the backup outbox row. Repeating the same report relies on existing primary and receiver idempotency constraints.
8. Standby resumes only after the pending count is zero. A failed sync enters `DEGRADED`; new local reports remain durably accepted while fallback service readiness remains true.

On a network partition during recovery, heartbeat freshness fails, state becomes `DEGRADED`, and queued reports stay on backup. If the primary has already persisted a report but its acknowledgement was lost, retry uses the same incident ID and receiver primary key, then returns the existing persistent receipt without creating a second incident.

## API and development controls

- `GET /api/failover/status` reads persisted runtime and outbox state.
- `POST /api/failover/heartbeat` validates a versioned authenticated heartbeat.
- `POST /api/fallback/sos` accepts local reports only while fallback service is active.
- `POST /api/failover/simulate` supports `heartbeat`, `drop_heartbeats`, `power_off`, `partition`, `restore_network`, `restore_primary`, `restart_backup`, `fail_next_transfer`, `lose_next_ack`, `sync`, and `tick`.

Simulation actions are available only in Flask tests or when both `NEXALERT_ENV=development` and `NEXALERT_ENABLE_FAILOVER_SIM=1` are set. The default operation does not instantiate a failover controller. The simulated implementation uses the simulated SOS transport and makes no radio claim.

## Split-brain and authentication limits

A two-node system cannot distinguish a failed master from a network partition using heartbeat absence alone. This implementation refuses promotion in the ambiguous case; the simulation's explicit power-off flag is not available evidence on real hardware. Do not enable automatic master duties on a real backup until the design includes a credible fence or arbitration policy. Options for evaluation include a lease backed by an independent witness/third node, a physically enforceable primary fence, or restricting backup duties to local emergency acceptance without asserting that it is command-centre master. A transient partition must not let both nodes write as authoritative masters.

HMAC authenticates heartbeat contents but does not encrypt them. Likewise, LoRa CRC/checksums detect transmission corruption but do not authenticate a sender or provide payload confidentiality. Key provisioning, rotation, replay protection across reboot, and incident confidentiality remain production security work.

## ESP32-S3 mapping and physical work still required

Future firmware can map the controller operations to an ESP32-S3 task, a monotonic timer, authenticated heartbeat adapter, local emergency HTTP service, and a durable storage adapter (for example, a carefully journaled flash/NVS or filesystem implementation). No GPIO map, radio band, output power, antenna, duty cycle, or radio library is specified here. The existing hardware inventory must be completed before wiring or RF tests.

Before calling failover physically verified, identify exact ESP32-S3 board and SX1262 carrier/revision; confirm power, antenna connector, regulatory configuration and applicable WPC requirements; measure heartbeat loss and activation timing; test actual Pi process/power failure separately from network partition; submit SOS with internet absent; power-cycle backup and verify its local queue; restore connectivity; prove same-ID persistence in the Pi receiver; repeat with lost acknowledgement; and capture logs and measured timing. The physical under-90-second DPR criterion remains unverified.
