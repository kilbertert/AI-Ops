# Production Access Boundary

TDengine Community Edition 3.4 allows a non-superuser to write into existing databases and rejects
`GRANT READ`. Production diagnostics therefore must not forward SSH directly to port 6041.

`aiops-tdengine-readonly-proxy.service` runs on the database host's loopback interface and accepts only
the exact `SHOW STABLES` and bounded `SELECT` statements emitted by this project. Its upstream TDengine
credential remains on that host. The engineer-facing SSH key can forward only to the proxy, MySQL, and
Redis diagnostic endpoints.

The proxy must be deployed with a root-owned environment file. Do not put either the client credential or
the upstream TDengine credential in this repository. Remove the service, service account, upstream user,
and SSH `permitopen` entry together when rolling back the production access path.
