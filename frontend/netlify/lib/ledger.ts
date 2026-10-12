import { createHash } from 'node:crypto'
import { and, desc, eq, inArray, or, sql } from 'drizzle-orm'
import { adjustments, auditLogs, batches, losses, notifications, stages, transfers, users } from '../../db/schema.js'
import type { Database, Transaction } from '../../db/index.js'
import type { AppUser, Stage } from './auth.js'
import { conflict, forbidden, notFound } from './errors.js'

type Connection = Database | Transaction
export type Batch = typeof batches.$inferSelect
export type Transfer = typeof transfers.$inferSelect
export const reservedStatuses = ['SENT', 'RECEIVED', 'DISPUTED', 'CONFIRMED'] as const

export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([first], [second]) => first.localeCompare(second)).map(([key, entry]) => `${JSON.stringify(key)}:${canonical(entry)}`).join(',')}}`
  return JSON.stringify(value) ?? 'null'
}

export function auditHash(previous: string, payload: unknown) {
  return createHash('sha256').update(previous + canonical(payload)).digest('hex')
}

export async function audit(tx: Transaction, user: AppUser, action: string, objectType: string, objectId: number | string, requestId: string, newValue: unknown = null, reason: string | null = null) {
  await tx.execute(sql`SELECT pg_advisory_xact_lock(71930421)`)
  const [previous] = await tx.select().from(auditLogs).orderBy(desc(auditLogs.id)).limit(1)
  const previousHash = previous?.event_hash || '0'.repeat(64)
  const payload = {
    action,
    object_type: objectType,
    object_id: String(objectId),
    actor_id: user.id,
    old_value: null,
    new_value: newValue,
    reason,
    created_at: new Date().toISOString(),
  }
  await tx.insert(auditLogs).values({ ...payload, request_id: requestId, previous_hash: previousHash, event_hash: auditHash(previousHash, payload) })
}

export async function verifyAudit(db: Database) {
  const rows = await db.select().from(auditLogs).orderBy(auditLogs.id)
  let previous = '0'.repeat(64)
  for (const row of rows) {
    const payload = {
      action: row.action,
      object_type: row.object_type,
      object_id: row.object_id,
      actor_id: row.actor_id,
      old_value: row.old_value,
      new_value: row.new_value,
      reason: row.reason,
      created_at: new Date(row.created_at).toISOString(),
    }
    if (row.previous_hash !== previous || auditHash(previous, payload) !== row.event_hash) return { valid: false, checked: rows.indexOf(row), broken_at: row.id }
    previous = row.event_hash
  }
  return { valid: true, checked: rows.length, broken_at: null }
}

export async function getBatch(db: Connection, batchId: number, lock = false) {
  const query = db.select().from(batches).where(eq(batches.id, batchId))
  const [batch] = await (lock ? query.for('update') : query)
  if (!batch) throw notFound()
  return batch
}

export async function availableQuantity(db: Connection, batch: Batch, stage: Stage) {
  const [transferRows, lossRows, adjustmentRows] = await Promise.all([
    db.select().from(transfers).where(eq(transfers.batch_id, batch.id)),
    db.select().from(losses).where(and(eq(losses.batch_id, batch.id), eq(losses.stage, stage), eq(losses.status, 'APPROVED'))),
    db.select().from(adjustments).where(and(eq(adjustments.batch_id, batch.id), eq(adjustments.stage, stage), eq(adjustments.status, 'APPROVED'))),
  ])
  const received = transferRows.filter(row => row.to_stage === stage && row.status === 'CONFIRMED').reduce((sum, row) => sum + (row.received_quantity || 0), 0)
  const sent = transferRows.filter(row => row.from_stage === stage && (reservedStatuses as readonly string[]).includes(row.status)).reduce((sum, row) => sum + row.sent_quantity, 0)
  const lost = lossRows.reduce((sum, row) => sum + row.quantity, 0)
  const corrected = adjustmentRows.reduce((sum, row) => sum + row.quantity_delta, 0)
  return (batch.stage === stage ? batch.initial_quantity : 0) + received - sent - lost + corrected
}

export async function requireBalance(db: Connection, batch: Batch, stage: Stage, quantity: number) {
  if (await availableQuantity(db, batch, stage) < quantity) throw conflict('INSUFFICIENT_BALANCE', 'Недостаточно доступного поголовья на этом этапе')
}

export async function requireBatchVisibility(db: Connection, batch: Batch, user: AppUser) {
  if (['ADMIN', 'MANAGER'].includes(user.role) || batch.stage === user.stage) return
  const [related] = await db.select({ id: transfers.id }).from(transfers).where(and(eq(transfers.batch_id, batch.id), or(eq(transfers.from_stage, user.stage!), eq(transfers.to_stage, user.stage!)))).limit(1)
  if (!related) throw forbidden()
}

export async function notifyStage(tx: Transaction, stage: Stage, title: string, body: string, kind: string, objectType: string, objectId: number) {
  const recipients = await tx.select({ id: users.id }).from(users).where(and(eq(users.is_active, true), or(and(eq(users.stage, stage), inArray(users.role, ['OPERATOR', 'SHIFT_LEAD'])), inArray(users.role, ['MANAGER', 'ADMIN']))))
  if (recipients.length) await tx.insert(notifications).values(recipients.map(user => ({ user_id: user.id, title, body, kind, object_type: objectType, object_id: objectId })))
}

export async function readLedger(db: Connection, user: AppUser) {
  const allStages = ['ADMIN', 'MANAGER'].includes(user.role)
  const batchVisibility = allStages ? undefined : or(eq(batches.stage, user.stage!), sql`EXISTS (SELECT 1 FROM ${transfers} WHERE ${transfers.batch_id} = ${batches.id} AND (${transfers.from_stage} = ${user.stage} OR ${transfers.to_stage} = ${user.stage}))`)
  const [batchRows, transferRows, lossRows, adjustmentRows] = await Promise.all([
    db.select().from(batches).where(batchVisibility).orderBy(desc(batches.created_at)),
    db.select().from(transfers).where(allStages ? undefined : or(eq(transfers.from_stage, user.stage!), eq(transfers.to_stage, user.stage!))).orderBy(desc(transfers.created_at)),
    db.select().from(losses).where(allStages ? undefined : eq(losses.stage, user.stage!)).orderBy(desc(losses.created_at)),
    db.select().from(adjustments).where(allStages ? undefined : eq(adjustments.stage, user.stage!)).orderBy(desc(adjustments.created_at)),
  ])
  return { batchRows, transferRows, lossRows, adjustmentRows }
}

export type Ledger = Awaited<ReturnType<typeof readLedger>>

export function stageBalance(ledger: Ledger, stage: Stage, batchId?: number) {
  const batchRows = ledger.batchRows.filter(row => batchId === undefined || row.id === batchId)
  const transferRows = ledger.transferRows.filter(row => batchId === undefined || row.batch_id === batchId)
  const lossRows = ledger.lossRows.filter(row => batchId === undefined || row.batch_id === batchId)
  const adjustmentRows = ledger.adjustmentRows.filter(row => batchId === undefined || row.batch_id === batchId)
  const opening = batchRows.filter(row => row.stage === stage).reduce((sum, row) => sum + row.initial_quantity, 0)
  const received = transferRows.filter(row => row.to_stage === stage && row.status === 'CONFIRMED').reduce((sum, row) => sum + (row.received_quantity || 0), 0)
  const transferred = transferRows.filter(row => row.from_stage === stage && (reservedStatuses as readonly string[]).includes(row.status)).reduce((sum, row) => sum + row.sent_quantity, 0)
  const lost = lossRows.filter(row => row.stage === stage && row.status === 'APPROVED').reduce((sum, row) => sum + row.quantity, 0)
  const corrected = adjustmentRows.filter(row => row.stage === stage && row.status === 'APPROVED').reduce((sum, row) => sum + row.quantity_delta, 0)
  return { stage, opening, received, transferred, losses: lost, available: opening + received - transferred - lost + corrected }
}

export function dashboard(ledger: Ledger, user: AppUser) {
  const discrepancies = ledger.transferRows.filter(row => (row.discrepancy || 0) > 0)
  const first = discrepancies.at(-1)
  return {
    stages: (user.stage ? [user.stage] : [...stages]).map(stage => stageBalance(ledger, stage)),
    active_batches: ledger.batchRows.filter(row => ['IN_PROGRESS', 'DISPUTED'].includes(row.status)).length,
    total_discrepancies: discrepancies.filter(row => row.status === 'CONFIRMED').reduce((sum, row) => sum + (row.discrepancy || 0), 0),
    first_discrepancy: first ? { stage: first.to_stage, quantity: first.discrepancy, transfer_number: first.transfer_number, created_at: first.created_at } : null,
    recent_discrepancies: discrepancies.slice(0, 10),
  }
}

export function analytics(ledger: Ledger, user: AppUser, params: URLSearchParams) {
  const stage = params.get('stage') || user.stage
  if (user.stage && stage !== user.stage) throw forbidden()
  const fromDate = params.get('from_date'), toDate = params.get('to_date')
  const batchId = Number(params.get('batch_id')), operatorId = Number(params.get('operator_id'))
  const matches = (row: { created_at: string; batch_id: number }) => (!fromDate || new Date(row.created_at) >= new Date(fromDate)) && (!toDate || new Date(row.created_at) <= new Date(toDate)) && (!batchId || row.batch_id === batchId)
  const transferRows = ledger.transferRows.filter(row => matches(row) && (!stage || row.to_stage === stage) && (!operatorId || row.sender_id === operatorId || row.receiver_id === operatorId))
  const lossRows = ledger.lossRows.filter(row => matches(row) && row.status === 'APPROVED' && (!stage || row.stage === stage) && (!operatorId || row.created_by === operatorId))
  const completed = transferRows.filter(row => row.status === 'CONFIRMED')
  const discrepancies = completed.filter(row => (row.discrepancy || 0) > 0)
  const sent = completed.reduce((sum, row) => sum + row.sent_quantity, 0)
  const lossQuantity = lossRows.reduce((sum, row) => sum + row.quantity, 0)
  const problemBatches = new Map<number, number>()
  const lossReasons = new Map<string, number>()
  for (const row of discrepancies) problemBatches.set(row.batch_id, (problemBatches.get(row.batch_id) || 0) + (row.discrepancy || 0))
  for (const row of lossRows) lossReasons.set(row.reason, (lossReasons.get(row.reason) || 0) + row.quantity)
  return {
    operation_count: transferRows.length,
    discrepancy_count: discrepancies.length,
    average_discrepancy: discrepancies.length ? Math.round(discrepancies.reduce((sum, row) => sum + (row.discrepancy || 0), 0) / discrepancies.length * 100) / 100 : 0,
    loss_quantity: lossQuantity,
    loss_percent: sent ? Math.round(lossQuantity / sent * 10000) / 100 : 0,
    problem_batches: [...problemBatches].map(([batch_id, discrepancy]) => ({ batch_id, discrepancy })).sort((first, second) => second.discrepancy - first.discrepancy).slice(0, 10),
    loss_reasons: [...lossReasons].map(([reason, quantity]) => ({ reason, quantity })),
  }
}
