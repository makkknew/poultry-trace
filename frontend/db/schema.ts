import { sql } from 'drizzle-orm'
import { boolean, check, index, integer, jsonb, pgEnum, pgTable, serial, text, timestamp, uuid, varchar } from 'drizzle-orm/pg-core'

export const stages = ['INCUBATION', 'GROWING', 'SLAUGHTER', 'PACKAGING'] as const
export const roles = ['OPERATOR', 'SHIFT_LEAD', 'MANAGER', 'ADMIN'] as const
export const stageEnum = pgEnum('production_stage', stages)
export const roleEnum = pgEnum('production_role', roles)
export const batchStatus = pgEnum('production_batch_status', ['CREATED', 'IN_PROGRESS', 'TRANSFERRED', 'RECEIVED', 'COMPLETED', 'DISPUTED', 'CLOSED'])
export const transferStatus = pgEnum('production_transfer_status', ['DRAFT', 'SENT', 'RECEIVED', 'DISPUTED', 'CONFIRMED', 'CANCELLED'])
export const reviewStatus = pgEnum('production_review_status', ['PENDING', 'APPROVED', 'REJECTED'])
export const lossReason = pgEnum('production_loss_reason', ['NATURAL_MORTALITY', 'DISEASE', 'INJURY', 'TECHNICAL_LOSS', 'OTHER'])

const createdAt = () => timestamp('created_at', { withTimezone: true, mode: 'string' }).notNull().defaultNow()
const eventTime = (name: string) => timestamp(name, { withTimezone: true, mode: 'string' })

export const users = pgTable('production_users', {
  id: serial('id').primaryKey(),
  identity_id: uuid('identity_id').notNull().unique(),
  username: varchar('username', { length: 254 }).notNull().unique(),
  full_name: varchar('full_name', { length: 160 }).notNull(),
  role: roleEnum('role').notNull(),
  stage: stageEnum('stage'),
  is_active: boolean('is_active').notNull().default(true),
  must_change_password: boolean('must_change_password').notNull().default(false),
  last_login_at: eventTime('last_login_at'),
  created_at: createdAt(),
}, table => [check('production_user_stage_role', sql`(${table.role} IN ('OPERATOR', 'SHIFT_LEAD') AND ${table.stage} IS NOT NULL) OR (${table.role} IN ('MANAGER', 'ADMIN') AND ${table.stage} IS NULL)`)])

export const batches = pgTable('production_batches', {
  id: serial('id').primaryKey(),
  batch_number: varchar('batch_number', { length: 40 }).notNull().unique(),
  stage: stageEnum('stage').notNull(),
  initial_quantity: integer('initial_quantity').notNull(),
  status: batchStatus('status').notNull().default('IN_PROGRESS'),
  notes: text('notes'),
  created_by: integer('created_by').notNull().references(() => users.id),
  created_at: createdAt(),
  updated_at: eventTime('updated_at').notNull().defaultNow(),
}, table => [check('production_batch_positive_quantity', sql`${table.initial_quantity} > 0`), index('production_batch_stage').on(table.stage)])

export const transfers = pgTable('production_transfers', {
  id: serial('id').primaryKey(),
  transfer_number: varchar('transfer_number', { length: 40 }).notNull().unique(),
  batch_id: integer('batch_id').notNull().references(() => batches.id),
  from_stage: stageEnum('from_stage').notNull(),
  to_stage: stageEnum('to_stage').notNull(),
  sent_quantity: integer('sent_quantity').notNull(),
  received_quantity: integer('received_quantity'),
  discrepancy: integer('discrepancy'),
  status: transferStatus('status').notNull().default('DRAFT'),
  sender_id: integer('sender_id').notNull().references(() => users.id),
  receiver_id: integer('receiver_id').references(() => users.id),
  confirmed_by: integer('confirmed_by').references(() => users.id),
  confirmation_comment: text('confirmation_comment'),
  rejection_reason: text('rejection_reason'),
  sent_at: eventTime('sent_at'),
  received_at: eventTime('received_at'),
  confirmed_at: eventTime('confirmed_at'),
  created_at: createdAt(),
}, table => [
  index('production_transfer_batch').on(table.batch_id),
  index('production_transfer_stage_status').on(table.from_stage, table.to_stage, table.status),
  check('production_transfer_positive_sent', sql`${table.sent_quantity} > 0`),
  check('production_transfer_received_range', sql`${table.received_quantity} IS NULL OR (${table.received_quantity} >= 0 AND ${table.received_quantity} <= ${table.sent_quantity})`),
  check('production_transfer_distinct_stages', sql`${table.from_stage} <> ${table.to_stage}`),
])

export const losses = pgTable('production_losses', {
  id: serial('id').primaryKey(),
  batch_id: integer('batch_id').notNull().references(() => batches.id),
  stage: stageEnum('stage').notNull(),
  quantity: integer('quantity').notNull(),
  reason: lossReason('reason').notNull(),
  comment: text('comment'),
  status: reviewStatus('status').notNull().default('PENDING'),
  created_by: integer('created_by').notNull().references(() => users.id),
  approved_by: integer('approved_by').references(() => users.id),
  approved_at: eventTime('approved_at'),
  created_at: createdAt(),
}, table => [index('production_loss_batch').on(table.batch_id), check('production_loss_positive', sql`${table.quantity} > 0`)])

export const adjustments = pgTable('production_balance_adjustments', {
  id: serial('id').primaryKey(),
  batch_id: integer('batch_id').notNull().references(() => batches.id),
  stage: stageEnum('stage').notNull(),
  quantity_delta: integer('quantity_delta').notNull(),
  reason: text('reason').notNull(),
  status: reviewStatus('status').notNull().default('PENDING'),
  created_by: integer('created_by').notNull().references(() => users.id),
  approved_by: integer('approved_by').references(() => users.id),
  approved_at: eventTime('approved_at'),
  created_at: createdAt(),
}, table => [index('production_adjustment_batch').on(table.batch_id), check('production_adjustment_nonzero', sql`${table.quantity_delta} <> 0`)])

export const notifications = pgTable('production_notifications', {
  id: serial('id').primaryKey(),
  user_id: integer('user_id').notNull().references(() => users.id),
  title: text('title').notNull(),
  body: text('body').notNull(),
  kind: varchar('kind', { length: 40 }).notNull(),
  object_type: varchar('object_type', { length: 40 }),
  object_id: integer('object_id'),
  read_at: eventTime('read_at'),
  created_at: createdAt(),
}, table => [index('production_notification_user').on(table.user_id)])

export const auditLogs = pgTable('production_audit_logs', {
  id: serial('id').primaryKey(),
  actor_id: integer('actor_id').references(() => users.id),
  action: varchar('action', { length: 80 }).notNull(),
  object_type: varchar('object_type', { length: 40 }).notNull(),
  object_id: varchar('object_id', { length: 80 }).notNull(),
  old_value: jsonb('old_value'),
  new_value: jsonb('new_value'),
  reason: text('reason'),
  request_id: varchar('request_id', { length: 64 }),
  previous_hash: varchar('previous_hash', { length: 64 }).notNull(),
  event_hash: varchar('event_hash', { length: 64 }).notNull().unique(),
  created_at: createdAt(),
}, table => [index('production_audit_created').on(table.created_at)])
