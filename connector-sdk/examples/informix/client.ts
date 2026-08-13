import * as odbc from 'odbc'

export interface InformixConnectionConfig {
  host: string
  port: number
  database: string
  server: string
  username: string
  password: string
  protocol?: 'onsoctcp' | 'onsoctcpssl'
  driverName?: string
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
  const driverName = config.driverName ?? 'IBM INFORMIX ODBC DRIVER'
  return [
    `Driver={${driverName}}`,
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

  // Fetches tables, all their columns, and all their primary keys in three
  // bulk queries instead of 1 + 2N — the N+1 version took 15+ minutes against
  // a ~500-table database, this takes a few seconds.
  async listReadableObjects(): Promise<InformixObject[]> {
    const connection = await this.getConnection()

    const tables = (await connection.query(
      `SELECT tabid, tabname FROM systables
       WHERE tabtype = 'T' AND tabid > 99
       ORDER BY tabname`,
    )) as Array<{ tabid: number; tabname: string }>

    const allColumns = (await connection.query(
      `SELECT c.tabid, c.colname, c.coltype, c.colno
       FROM syscolumns c
       JOIN systables t ON t.tabid = c.tabid
       WHERE t.tabtype = 'T' AND t.tabid > 99
       ORDER BY c.tabid, c.colno`,
    )) as Array<{ tabid: number; colname: string; coltype: number; colno: number }>

    const allPrimaryKeyParts = (await connection.query(
      `SELECT con.tabid, i.part1, i.part2, i.part3, i.part4, i.part5, i.part6, i.part7, i.part8
       FROM sysconstraints con
       JOIN sysindexes i ON i.idxname = con.idxname AND i.tabid = con.tabid
       JOIN systables t ON t.tabid = con.tabid
       WHERE con.constrtype = 'P' AND t.tabtype = 'T' AND t.tabid > 99`,
    )) as Array<Record<string, number>>

    const columnsByTable = new Map<number, Array<{ colname: string; coltype: number; colno: number }>>()
    const colnoToNameByTable = new Map<number, Map<number, string>>()
    for (const column of allColumns) {
      if (!columnsByTable.has(column.tabid)) columnsByTable.set(column.tabid, [])
      columnsByTable.get(column.tabid)!.push(column)
      if (!colnoToNameByTable.has(column.tabid)) colnoToNameByTable.set(column.tabid, new Map())
      colnoToNameByTable.get(column.tabid)!.set(column.colno, column.colname.trim())
    }

    const primaryKeyByTable = new Map<number, string[]>()
    for (const row of allPrimaryKeyParts) {
      const { tabid, ...parts } = row
      const colnoToName = colnoToNameByTable.get(tabid)
      if (!colnoToName) continue
      const partColumnNumbers = Object.values(parts).filter((part): part is number => Boolean(part))
      const names = partColumnNumbers
        .map((part) => colnoToName.get(part))
        .filter((name): name is string => Boolean(name))
      if (names.length > 0) primaryKeyByTable.set(tabid, names)
    }

    return tables.map((table) => {
      const columns = columnsByTable.get(table.tabid) ?? []
      return {
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
        primaryKey: primaryKeyByTable.get(table.tabid),
      }
    })
  }

  // `object` is validated by previewRows() against ^[a-zA-Z_][a-zA-Z0-9_]*$
  // before this is called, so it's safe to interpolate directly. Binding it
  // as a query parameter instead trips a driver bug (ODBC HY003 "Invalid
  // application buffer type") in the Informix ODBC driver's string handling.
  private async isReadableTable(object: string): Promise<boolean> {
    const connection = await this.getConnection()
    const rows = (await connection.query(
      `SELECT FIRST 1 tabid FROM systables WHERE TRIM(tabname) = '${object}' AND tabtype = 'T' AND tabid > 99`,
    )) as Array<{ tabid: number }>
    return rows.length > 0
  }

  async previewRows(object: string, options: { limit: number }): Promise<Array<Record<string, unknown>>> {
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(object)) {
      throw new Error('Invalid object name')
    }

    const connection = await this.getConnection()
    if (!(await this.isReadableTable(object))) {
      throw new Error(`Object "${object}" is not a readable table`)
    }

    const limit = Math.min(Math.max(options.limit, 1), 25)
    return (await connection.query(`SELECT FIRST ${limit} * FROM ${object}`)) as Array<Record<string, unknown>>
  }
}
