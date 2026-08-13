import { readFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
import type { ConnectorManifest } from './types'

async function main() {
  const manifestPath = process.argv[2]
  assert.ok(manifestPath, 'Usage: tsx connector-sdk/validate.ts <manifest.json>')

  const manifest = JSON.parse(await readFile(manifestPath, 'utf8')) as ConnectorManifest
  assert.match(manifest.id, /^[a-z0-9]+(?:-[a-z0-9]+)*$/, 'id must be lowercase kebab-case')
  assert.ok(manifest.name.trim().length >= 3, 'name is required')
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/, 'version must use semantic versioning')
  assert.ok(manifest.publisher.trim().length >= 2, 'publisher is required')
  assert.equal(manifest.access, 'read-only', 'Marketplace connectors must be read-only')
  assert.ok(['https-api', 'database', 'private-agent'].includes(manifest.transport), 'unsupported transport')
  assert.match(manifest.documentationUrl, /^https:\/\//, 'documentationUrl must use HTTPS')

  console.log(JSON.stringify({
    ok: true,
    connector: manifest.id,
    name: manifest.name,
    version: manifest.version,
    access: manifest.access,
    transport: manifest.transport,
    checks: [
      'manifest-shape',
      'semantic-version',
      'read-only-access',
      'supported-transport',
      'https-documentation',
    ],
  }, null, 2))
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error)
  process.exitCode = 1
})
