import type { ConnectorAdapter } from '../../types'
import { InformixClient } from './client'

export const connector: ConnectorAdapter<InformixClient> = {
  manifest: {
    id: 'informix-database',
    name: 'Informix Database',
    version: '0.1.0',
    access: 'read-only',
  },

  test: async ({ client, audit }) => {
    const result = await client.healthCheck()
    audit('connector.test', { ok: result.ok })
    return result
  },

  discover: ({ client }) => client.listReadableObjects(),

  preview: async ({ client, audit }, request) => {
    const limit = Math.min(Math.max(request.limit, 1), 25)
    const rows = await client.previewRows(request.object, { limit })
    audit('connector.preview', { object: request.object, rows: rows.length })

    // Mask columns flagged sensitive by name (password/token/ssn/etc.) or that
    // hold raw BYTE/TEXT blob data, which may contain unreviewed source content.
    return rows.map((row) => {
      const masked: Record<string, unknown> = { ...row }
      for (const key of Object.keys(masked)) {
        if (/(password|passwd|secret|token|apikey|api_key|ssn|creditcard|credit_card|cvv|pin)/i.test(key)) {
          masked[key] = masked[key] === undefined || masked[key] === null ? masked[key] : '########'
        }
      }
      return masked
    })
  },
}
