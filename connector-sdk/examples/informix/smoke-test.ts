import 'dotenv/config'
import { connector } from './adapter'
import { InformixClient } from './client'

async function main() {
  const client = new InformixClient({
    host: requireEnv('INFORMIX_HOST'),
    port: Number(requireEnv('INFORMIX_PORT')),
    server: requireEnv('INFORMIX_SERVER'),
    database: requireEnv('INFORMIX_DATABASE'),
    username: requireEnv('INFORMIX_USER'),
    password: requireEnv('INFORMIX_PASSWORD'),
    driverName: process.env.INFORMIX_DRIVER,
  })

  const context = {
    tenantId: 'local-smoke-test',
    client,
    signal: new AbortController().signal,
    audit: (event: string, details?: Record<string, string | number | boolean>) =>
      console.log('[audit]', event, details ?? {}),
  }

  const testResult = await connector.test(context)
  console.log('test():', testResult)
  if (!testResult.ok) return

  const objects = await connector.discover(context)
  console.log(`discover(): found ${objects.length} table(s)`)
  console.log(JSON.stringify(objects.slice(0, 5), null, 2))

  if (process.env.SKIP_PREVIEW !== '1') {
    const previewObject = process.env.PREVIEW_OBJECT ?? objects[0]?.name
    if (previewObject) {
      const preview = await connector.preview(context, { object: previewObject, limit: 5 })
      console.log(`preview("${previewObject}"):`, preview)
    }
  }

  await client.close()
}

function requireEnv(name: string): string {
  const value = process.env[name]
  if (!value) throw new Error(`Missing required env var: ${name}`)
  return value
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
