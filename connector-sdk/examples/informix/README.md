# Informix connector

Read-only connector for IBM Informix (tested against 9.4) using the `odbc`
npm package and the IBM Informix ODBC driver (CSDK).

## Prerequisites

1. Install the IBM Informix Client SDK (CSDK) for your platform, which provides
   the `IBM INFORMIX ODBC DRIVER`. The `odbc` npm package links against it at
   install time, so it must be present before `npm install` / `pnpm install`.
2. A **dedicated, read-only** Informix service account. Do not use `informix`,
   `root`, or any account with write/DDL privileges.
3. Network access from this machine to the Informix server:
   - Direct connection: `host:port` reachable normally.
   - Tunnelled connection (e.g. via `ngrok tcp <port>`): use the ngrok-assigned
     host/port in place of the local ones. The adapter doesn't care how the
     TCP path is established, only that `host`/`port` are reachable.

   Note: exposing a database directly on ngrok's public internet is a real
   attack surface (port scanners find ngrok ranges quickly). For anything
   beyond a quick local smoke test, restrict access with ngrok's IP allowlist
   or basic-auth/OAuth features, and keep the service account strictly
   read-only.

## Setup

1. Copy `.env.example` to `.env` and fill in your connection details.
2. Install dependencies:

```bash
pnpm install
```

3. Run the smoke test to exercise `test`, `discover`, and `preview` against
   your real server:

```bash
pnpm exec tsx connector-sdk/examples/informix/smoke-test.ts
```

4. Validate the manifest:

```bash
pnpm exec tsx connector-sdk/validate.ts connector-sdk/examples/informix/manifest.json
```

## Notes on the implementation

- `discover()` reads Informix system catalog tables (`systables`,
  `syscolumns`, `sysconstraints`, `sysindexes`) to enumerate user tables
  (`tabid > 99`, `tabtype = 'T'`), their columns, and primary keys. It does
  not read row data.
- `preview()` validates the requested object name against the list returned
  by `discover()` before interpolating it into SQL (Informix does not support
  parameterized table identifiers), and caps rows at 25 via `SELECT FIRST n`.
- Columns whose name matches common secret/PII patterns (password, token,
  ssn, etc.) are marked `sensitive` in `discover()` and masked in `preview()`.
  Review this against your actual schema — name-based heuristics will miss
  columns that don't follow the convention.
- Update `manifest.json` — set a real `publisher` and, once you have one, a
  more specific `id`/`documentationUrl`, before submitting to Connector
  Studio.

## Coltype mapping caveat

The Informix `coltype` mapping in `client.ts` covers the common scalar types
(CHAR/VARCHAR/NCHAR, INTEGER/SMALLINT/DECIMAL/SERIAL family, DATE/DATETIME,
BOOLEAN, TEXT/BYTE, and collection/ROW types). If your schema uses extended
or opaque user-defined types, they'll surface as `unknown` — check
`syscolumns.extended_id` against `sysxtdtypes` if you need finer-grained
mapping.
