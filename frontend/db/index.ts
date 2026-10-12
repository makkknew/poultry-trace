import { drizzle } from 'drizzle-orm/netlify-db'
import * as schema from './schema.js'

export const getDb = () => drizzle({ schema })
export type Database = ReturnType<typeof getDb>
export type Transaction = Parameters<Parameters<Database['transaction']>[0]>[0]
