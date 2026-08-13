# datahuts Connector SDK

This starter is for customer and partner teams building governed, read-only
connectors for any approved source: databases, data warehouses, SaaS
applications, APIs, structured files, private systems, and legacy ERPs.

## Required adapter contract

Implement:

1. `test` — verify authentication and read access without changing source data.
2. `discover` — return accessible objects, fields, keys, and relationships.
3. `preview` — return a small, masked sample for an explicitly selected object.

The connector must not accept arbitrary SQL from a browser, implement write
operations, log credentials, or return credentials in an API response.

## Self-service development workflow

Download the starter from the SDK documentation or copy
`examples/custom-source`. Rename it for your source, implement the three methods
against synthetic or non-customer test data, and run:

```bash
pnpm exec tsx connector-sdk/validate.ts connector-sdk/examples/custom-source/manifest.json
```

Then sign in to datahuts and open **Developers → Connector Studio**. Create a
connector key, submit the manifest, repository URL, and validation output, and
track the review from the same screen. Email is not required.

Shared marketplace publication remains gated: datahuts reviews security,
tenant isolation, metadata quality, failure handling, and customer-facing copy
before making an adapter available to other tenants.

## Private networks

For systems that cannot accept inbound traffic, package the adapter with the
datahuts private connector agent. The agent runs inside the customer network,
connects locally to the source, and opens outbound HTTPS only to datahuts.
