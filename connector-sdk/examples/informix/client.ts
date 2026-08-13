import * as odbc from 'odbc'

export interface InformixConnectionConfig {
  host: string
  port: number
  database: string
  server: string
  username: string
  password: string
  protocol?: 'onsoctcp' | 'onsoctcpssl'
}

export interface InformixField {
  name: string
  type: 'string' | 'number' | 'boolean' | 'date' | 'json' | 'unknown'
  nullable: boolean
  sensitive?: boolean
}

export interface InformixObject {
  name: string
  label: string
  fields: InformixField[]
  primaryKey?: string[]
}

const SENSITIVE_NAME_PATTERN = /(password|passwd|secret|token|apikey|api_key|ssn|creditcard|credit_card|cvv|pin)/i

function buildConnectionString(config: InformixConnectionConfig): string {
  const protocol = config.protocol ?? 'onsoctcp'
  return [
    `Driver={IBM INFORMIX ODBC DRIVER}`,
    `Host=${config.host}`,
    `Service=${config.port}`,
    `Server=${config.server}`,
    `Database=${config.database}`,
    `Protocol=${protocol}`,
    `Uid=${config.username}`,
    `Pwd=${config.password}`,
  ].join(';')
}

// Informix syscolumns.coltype packs the base type in the low byte; a value of
// 256+ marks the column as nullable. See "Informix Guide to SQL: Reference"
// for the full coltype table — this covers the common data types only.
function mapColumnType(coltype: number): { type: InformixField['type']; nullable: boolean } {
  const nullable = coltype >= 256
  const base = coltype % 256
  const numeric = new Set([1, 2, 3, 4, 5, 6, 17, 18, 52, 53])
  const text = new Set([0, 13, 15, 16, 40, 43])
  const dateLike = new Set([7, 10])
  if (numeric.has(base)) return { type: 'number', nullable }
  if (text.has(base)) return { type: 'string', nullable }
  if (dateLike.has(base)) return { type: 'date', nullable }
  if (base === 45) return { type: 'boolean', nullable }
  if (base === 12 || base === 11) return { type: 'string', nullable }
  if (base === 22 || base === 19 || base === 20 || base === 21) return { type: 'json', nullable }
  return { type: 'unknown', nullable }
}

export class InformixClient {
  private connection: odbc.Connection | undefined
  private readonly config: InformixConnectionConfig

  constructor(config: InformixConnectionConfig) {
    this.config = config
  }

  private async getConnection(): Promise<odbc.Connection> {
    if (!this.connection) {
      this.connection = await odbc.connect(buildConnectionString(this.config))
    }
    return this.connection
  }

  async close(): Promise<void> {
    if (this.connection) {
      await this.connection.close()
      this.connection = undefined
    }
  }

  async healthCheck(): Promise<{ ok: boolean; message: string }> {
    try {
      const connection = await this.getConnection()
      await connection.query('SELECT FIRST 1 tabid FROM systables')
      return { ok: true, message: 'Connected to Informix and read systables.' }
    } catch (error) {
      return { ok: false, message: error instanceof Error ? error.message : 'Unknown connection error' }
    }
  }

  async listReadableObjects(): Promise<InformixObject[]> {
    const connection = await this.getConnection()

    const tables = (await connection.query(
      `SELECT tabname, owner, tabid FROM systables
       WHERE tabtype = 'T' AND tabid > 99
       ORDER BY tabname`,
    )) as Array<{ tabname: string; owner: string; tabid: number }>

    const objects: InformixObject[] = []

    for (const table of tables) {
      const columns = (await connection.query(
        `SELECT colname, coltype, colno FROM syscolumns
         WHERE tabid = ? ORDER BY colno`,
        [table.tabid],
      )) as Array<{ colname: string; coltype: number; colno: number }>

      const primaryKeyColumns = await this.getPrimaryKeyColumns(table.tabid)

      objects.push({
        name: table.tabname.trim(),
        label: table.tabname.trim(),
        fields: columns.map((column) => {
          const { type, nullable } = mapColumnType(column.coltype)
          const name = column.colname.trim()
          return {
            name,
            type,
            nullable,
            sensitive: SENSITIVE_NAME_PATTERN.test(name) || undefined,
          }
        }),
        primaryKey: primaryKeyColumns.length > 0 ? primaryKeyColumns : undefined,
      })
    }

    return objects
  }

  private async getPrimaryKeyColumns(tabid: number): Promise<string[]> {
    const connection = await this.getConnection()

    const indexes = (await connection.query(
      `SELECT i.part1, i.part2, i.part3, i.part4, i.part5, i.part6, i.part7, i.part8
       FROM sysconstraints c
       JOIN sysindexes i ON i.idxname = c.idxname AND i.tabid = c.tabid
       WHERE c.tabid = ? AND c.constrtype = 'P'`,
      [tabid],
    )) as Array<Record<string, number>>

    if (indexes.length === 0) return []

    const columns = (await connection.query(
      `SELECT colno, colname FROM syscolumns WHERE tabid = ?`,
      [tabid],
    )) as Array<{ colno: number; colname: string }>
    const colnoToName = new Map(columns.map((column) => [column.colno, column.colname.trim()]))

    const partColumnNumbers = Object.values(indexes[0]).filter((part): part is number => Boolean(part))
    return partColumnNumbers
      .map((part) => colnoToName.get(part))
      .filter((name): name is string => Boolean(name))
  }

  async previewRows(object: string, options: { limit: number }): Promise<Array<Record<string, unknown>>> {
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(object)) {
      throw new Error('Invalid object name')
    }

    const connection = await this.getConnection()
    const validObjects = await this.listReadableObjects()
    if (!validObjects.some((candidate) => candidate.name === object)) {
      throw new Error(`Object "${object}" is not a readable table`)
    }

    const limit = Math.min(Math.max(options.limit, 1), 25)
    return (await connection.query(`SELECT FIRST ${limit} * FROM ${object}`)) as Array<Record<string, unknown>>
  }
}
