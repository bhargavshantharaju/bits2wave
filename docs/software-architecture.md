# Current software architecture

```mermaid
flowchart LR
  Citizen[Citizen browser] -->|HTTP session + CSRF| Web[Single Flask Socket.IO process]
  Web -->|transaction| Sender[(Sender SQLite)]
  Sender --> Outbox[Bounded persistent outbox]
  Worker[Single supervised outbox worker] --> Outbox
  Worker --> Transport[SimulatedTransport by default]
  Transport -->|commit incident, then receipt| CC[(Independent command-centre SQLite)]
  CC --> Dashboard[Operator dashboard APIs]
  Backup[Fallback SQLite node simulation] -->|signed ownership assertion + same incident ID| Web
  Radio[Future radio driver: disabled] -.-> Transport
```

```mermaid
sequenceDiagram
  participant C as Citizen browser
  participant A as Flask application
  participant S as Sender SQLite
  participant W as Outbox worker
  participant D as Command-centre store
  C->>A: POST SOS with stable incident ID
  A->>S: Commit incident + queued outbox record
  S-->>A: Durable commit
  A-->>C: 201 queued
  W->>S: Select one bounded, due record
  W->>D: Validate and insert idempotently
  D->>D: Commit incident and receipt
  D-->>W: Correlated durable receipt
  W->>S: Mark received and record attempt
  A-->>C: Later status read shows receipt
```

The software simulator does not model LoRa delivery, route diversity, RF timing, link authentication, or physical failover. The separately stored command-centre database is durable, but not every dashboard read and lifecycle mutation has yet been moved there; see [the completion matrix](software-completion-matrix.md).

## Supported process topology

Run one web process and exactly one worker per local SQLite deployment. Both entry points use host-local advisory lock files derived from the sender database path. This blocks accidental second instances on one host, but does not coordinate across hosts or filesystems. Do not run multiple WSGI workers or replicas against the same SQLite files. A database server or distributed Socket.IO coordination would be a separate architecture change.
