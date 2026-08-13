# Custom source starter

1. Rename the connector in `manifest.json` and `adapter.ts`.
2. Implement the client methods against synthetic or non-customer test data.
3. Mark sensitive discovered fields and mask all preview values that may contain
   credentials, tokens, payment data, or personal information.
4. Validate the manifest:

```bash
pnpm exec tsx connector-sdk/validate.ts connector-sdk/examples/custom-source/manifest.json
```

5. Run your adapter tests, then submit the manifest, repository URL, and test
   summary from **Dashboard → Developers → Connector Studio**.
