export interface ConnectorManifest {
  id: string
  name: string
  version: string
  publisher: string
  access: 'read-only'
  transport: 'https-api' | 'database' | 'private-agent'
  documentationUrl: string
}

export interface DiscoveredField {
  name: string
  type: 'string' | 'number' | 'boolean' | 'date' | 'json' | 'unknown'
  nullable: boolean
  sensitive?: boolean
}

export interface DiscoveredObject {
  name: string
  label: string
  fields: DiscoveredField[]
  primaryKey?: string[]
}

export interface ConnectorContext<Client = unknown> {
  tenantId: string
  client: Client
  signal: AbortSignal
  audit: (event: string, details?: Record<string, string | number | boolean>) => void
}

export interface ConnectorAdapter<Client = unknown> {
  manifest: Pick<ConnectorManifest, 'id' | 'name' | 'version' | 'access'>
  test(context: ConnectorContext<Client>): Promise<{ ok: boolean; message: string }>
  discover(context: ConnectorContext<Client>): Promise<DiscoveredObject[]>
  preview(
    context: ConnectorContext<Client>,
    request: { object: string; limit: number },
  ): Promise<Array<Record<string, unknown>>>
}
