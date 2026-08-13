import type { ConnectorAdapter } from '../../types'

interface AcmeClient {
  healthCheck(): Promise<{ ok: boolean; message: string }>
  listReadableObjects(): Promise<Array<{
    name: string
    label: string
    fields: Array<{
      name: string
      type: 'string' | 'number' | 'boolean' | 'date' | 'json' | 'unknown'
      nullable: boolean
      sensitive?: boolean
    }>
    primaryKey?: string[]
  }>>
  previewRows(
    object: string,
    options: { limit: number },
  ): Promise<Array<Record<string, unknown>>>
}

export const connector: ConnectorAdapter<AcmeClient> = {
  manifest: {
    id: 'acme-operations',
    name: 'Acme Operations',
    version: '0.1.0',
    access: 'read-only',
  },

  test: async ({ client, audit }) => {
    const result = await client.healthCheck()
    audit('connector.test', { ok: result.ok })
    return result
  },

  discover: ({ client }) => client.listReadableObjects(),

  preview: async ({ client }, request) => {
    const limit = Math.min(Math.max(request.limit, 1), 25)
    const rows = await client.previewRows(request.object, { limit })

    // Replace this example with source-specific masking. Never return secrets,
    // access tokens, passwords, full payment details, or unrestricted PII.
    return rows.map((row) => ({
      ...row,
      password: row.password === undefined ? undefined : '########',
      accessToken: row.accessToken === undefined ? undefined : '########',
    }))
  },
}
