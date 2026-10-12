import { z } from 'zod'
import { lossReason, roles, stages } from '../../db/schema.js'

export const stageSchema = z.enum(stages)
export const roleSchema = z.enum(roles)
export const quantity = z.number().int().min(1).max(2_000_000_000)
export const passwordSchema = z.string().min(12).max(128).regex(/[a-z]/).regex(/[A-Z]/).regex(/[0-9]/)
export const batchInput = z.object({
  batch_number: z.string().min(3).max(40).regex(/^[A-Za-z0-9_-]+$/),
  stage: stageSchema,
  initial_quantity: quantity,
  notes: z.string().max(1000).nullable().optional(),
})
export const transferInput = z.object({ batch_id: quantity, sent_quantity: quantity })
export const receiveInput = z.object({ received_quantity: z.number().int().min(0).max(2_000_000_000), comment: z.string().max(1000).nullable().optional() })
export const reviewInput = z.object({ reason: z.string().max(1000).nullable().optional() })
export const lossInput = z.object({
  batch_id: quantity,
  quantity,
  reason: z.enum(lossReason.enumValues),
  comment: z.string().max(1000).nullable().optional(),
}).refine(value => value.reason !== 'OTHER' || Boolean(value.comment?.trim()), { message: 'Укажите комментарий для причины «Другое»' })
export const adjustmentInput = z.object({
  batch_id: quantity,
  quantity_delta: z.number().int().min(-2_000_000_000).max(2_000_000_000).refine(value => value !== 0),
  reason: z.string().trim().min(5).max(1000),
})
export const userInput = z.object({
  email: z.email().max(254).transform(value => value.trim().toLowerCase()),
  full_name: z.string().trim().min(2).max(160),
  password: passwordSchema,
  role: roleSchema,
  stage: stageSchema.nullable(),
}).refine(value => ['ADMIN', 'MANAGER'].includes(value.role) ? value.stage === null : value.stage !== null, { message: 'Производственной роли нужен этап' })
