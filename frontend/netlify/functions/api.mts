import { randomUUID } from 'node:crypto'
import { admin, AuthError, MissingIdentityError, verifyRequestOrigin } from '@netlify/identity'
import type { Config, Context } from '@netlify/functions'
import { and, desc, eq, isNull, sql } from 'drizzle-orm'
import { z } from 'zod'
import { getDb } from '../../db/index.js'
import type { Database } from '../../db/index.js'
import { adjustments, auditLogs, batches, losses, notifications, stages, transfers, users } from '../../db/schema.js'
import { authenticate, publicUser, requireProduction, requireReviewer, requireRole, requireStage } from '../lib/auth.js'
import type { AppUser } from '../lib/auth.js'
import { ApiError, conflict, forbidden, notFound } from '../lib/errors.js'
import { analytics, audit, availableQuantity, dashboard, getBatch, notifyStage, readLedger, requireBalance, requireBatchVisibility, stageBalance, verifyAudit } from '../lib/ledger.js'
import { adjustmentInput, batchInput, lossInput, passwordSchema, quantity, receiveInput, reviewInput, roleSchema, stageSchema, transferInput, userInput } from '../lib/validation.js'
import { exportReport } from '../lib/export.js'

async function input<T>(request: Request, schema: z.ZodType<T>) {
  if (!request.headers.get('content-type')?.includes('application/json')) throw new ApiError(415, 'JSON_REQUIRED', 'Отправьте данные в формате JSON')
  if (Number(request.headers.get('content-length') || 0) > 16_384) throw new ApiError(413, 'BODY_TOO_LARGE', 'Слишком большой запрос')
  const body = await request.text()
  if (body.length > 16_384) throw new ApiError(413, 'BODY_TOO_LARGE', 'Слишком большой запрос')
  let data: unknown
  try { data = JSON.parse(body) } catch { throw new ApiError(400, 'INVALID_JSON', 'Некорректный JSON') }
  return schema.parse(data)
}

function routeId(value: string) {
  return quantity.parse(Number(value))
}

function paginate<T extends { status?: string }>(rows: T[], params: URLSearchParams) {
  const limit = z.coerce.number().int().min(1).max(500).parse(params.get('limit') || 100)
  const offset = z.coerce.number().int().min(0).parse(params.get('offset') || 0)
  const status = params.get('status')
  return rows.filter(row => !status || row.status === status).slice(offset, offset + limit)
}

async function readRoute(db: Database, user: AppUser, path: string, params: URLSearchParams): Promise<unknown | Response> {
  if (path === '/auth/me') return publicUser(user)
  if (path === '/users') {
    requireRole(user, ['ADMIN', 'MANAGER'])
    return (await db.select().from(users).orderBy(users.id)).map(publicUser)
  }
  if (path === '/audit-logs/verify') {
    requireRole(user, ['ADMIN'])
    return verifyAudit(db)
  }
  if (path === '/audit-logs') {
    requireRole(user, ['ADMIN', 'MANAGER'])
    const limit = z.coerce.number().int().min(1).max(500).parse(params.get('limit') || 100)
    const offset = z.coerce.number().int().min(0).parse(params.get('offset') || 0)
    return db.select().from(auditLogs).orderBy(desc(auditLogs.id)).limit(limit).offset(offset)
  }
  if (path === '/notifications') {
    return db.select().from(notifications).where(and(eq(notifications.user_id, user.id), params.get('unread_only') === 'true' ? isNull(notifications.read_at) : undefined)).orderBy(desc(notifications.created_at)).limit(100)
  }
  if (path.startsWith('/export/')) {
    requireRole(user, ['ADMIN', 'MANAGER'])
    const reportType = z.enum(['batches', 'transfers', 'losses', 'discrepancies', 'audit']).parse(path.slice('/export/'.length))
    const format = z.enum(['csv', 'xlsx', 'pdf']).parse(params.get('format') || 'csv')
    const ledger = reportType === 'audit' ? null : await readLedger(db, user)
    const records = reportType === 'audit' ? await db.select().from(auditLogs).orderBy(auditLogs.id)
      : reportType === 'batches' ? ledger!.batchRows
      : reportType === 'losses' ? ledger!.lossRows
      : reportType === 'discrepancies' ? ledger!.transferRows.filter(row => (row.discrepancy || 0) > 0)
      : ledger!.transferRows
    return exportReport(records, reportType, format)
  }
  const detail = path.match(/^\/batches\/(\d+)$/)
  if (detail) {
    const batch = await getBatch(db, routeId(detail[1]))
    await requireBatchVisibility(db, batch, user)
    const ledger = await readLedger(db, user)
    return { ...batch, transfers: ledger.transferRows.filter(row => row.batch_id === batch.id).reverse(), losses: ledger.lossRows.filter(row => row.batch_id === batch.id).reverse() }
  }
  if (!['/batches', '/transfers', '/losses', '/balance-adjustments', '/dashboard', '/analytics', '/alerts', '/search'].includes(path)) throw notFound()
  const ledger = await readLedger(db, user)
  const term = (params.get('q') || '').slice(0, 80).toLowerCase()
  if (path === '/batches') return paginate(ledger.batchRows.filter(row => row.batch_number.toLowerCase().includes(term)), params).map(row => ({ ...row, available_quantity: stageBalance(ledger, user.stage || row.stage, row.id).available }))
  if (path === '/transfers') return paginate(ledger.transferRows.filter(row => row.transfer_number.toLowerCase().includes(term)), params)
  if (path === '/losses') return paginate(ledger.lossRows, params)
  if (path === '/balance-adjustments') return paginate(ledger.adjustmentRows, params)
  if (path === '/dashboard') return dashboard(ledger, user)
  if (path === '/analytics') return analytics(ledger, user, params)
  if (path === '/search') return { batches: ledger.batchRows.filter(row => row.batch_number.toLowerCase().includes(term)).slice(0, 20), transfers: ledger.transferRows.filter(row => row.transfer_number.toLowerCase().includes(term)).slice(0, 20) }
  const discrepancyRows = ledger.transferRows.filter(row => (row.discrepancy || 0) > 0)
  const alerts: unknown[] = discrepancyRows.map(row => ({ severity: row.discrepancy! < 3 ? 'LOW' : row.discrepancy! < 10 ? 'MEDIUM' : 'HIGH', message: `Расхождение ${row.discrepancy} голов требует проверки`, transfer: row }))
  for (const stage of stages) {
    const count = discrepancyRows.filter(row => row.to_stage === stage).length
    if (count >= 3) alerts.push({ severity: 'CRITICAL', message: `Повторяющиеся расхождения на этапе ${stage}: ${count}`, stage })
  }
  return alerts
}

async function writeRoute(db: Database, user: AppUser, request: Request, path: string, requestId: string) {
  if (path === '/auth/password-changed') {
    await db.transaction(async tx => {
      await tx.update(users).set({ must_change_password: false }).where(eq(users.id, user.id))
      await audit(tx, user, 'PASSWORD_CHANGED', 'user', user.id, requestId)
    })
    return { success: true }
  }
  if (path === '/notifications' || path === '/auth/login' || path === '/auth/refresh' || path === '/auth/logout') throw new ApiError(410, 'USE_NETLIFY_IDENTITY', 'Вход и восстановление пароля теперь выполняются через Netlify Identity')
  const notificationRoute = path.match(/^\/notifications\/(\d+)\/read$/)
  if (notificationRoute) {
    const [updated] = await db.update(notifications).set({ read_at: new Date().toISOString() }).where(and(eq(notifications.id, routeId(notificationRoute[1])), eq(notifications.user_id, user.id))).returning()
    if (!updated) throw notFound()
    return { success: true }
  }
  if (path === '/users') {
    requireRole(user, ['ADMIN'])
    const data = await input(request, userInput)
    const identity = await admin.createUser({ email: data.email, password: data.password, data: { app_metadata: { roles: [data.role], authorization: { roles: [data.role] }, stage: data.stage }, user_metadata: { full_name: data.full_name } } })
    try {
      return await db.transaction(async tx => {
        const [created] = await tx.insert(users).values({ identity_id: identity.id, username: data.email, full_name: data.full_name, role: data.role, stage: data.stage, must_change_password: true }).returning()
        await audit(tx, user, 'USER_CREATED', 'user', created.id, requestId, { role: data.role, stage: data.stage })
        return publicUser(created)
      })
    } catch (error) {
      try { await admin.deleteUser(identity.id) } catch {}
      throw error
    }
  }
  const userRoute = path.match(/^\/users\/(\d+)(?:\/(disable|reset-password))?$/)
  if (userRoute) {
    requireRole(user, ['ADMIN'])
    const userId = routeId(userRoute[1])
    const [target] = await db.select().from(users).where(eq(users.id, userId))
    if (!target) throw notFound()
    if (userRoute[2] === 'disable') {
      if (userId === user.id) throw conflict('CANNOT_DISABLE_SELF', 'Нельзя отключить собственную учетную запись')
      await db.transaction(async tx => {
        await tx.update(users).set({ is_active: false }).where(eq(users.id, userId))
        await audit(tx, user, 'USER_BLOCKED', 'user', userId, requestId)
      })
      return { success: true }
    }
    if (userRoute[2] === 'reset-password') {
      const data = await input(request, z.object({ new_password: passwordSchema }))
      await admin.updateUser(target.identity_id, { password: data.new_password })
      await db.transaction(async tx => {
        await tx.update(users).set({ must_change_password: true }).where(eq(users.id, userId))
        await audit(tx, user, 'PASSWORD_RESET_BY_ADMIN', 'user', userId, requestId)
      })
      return { success: true, must_change_password: true }
    }
    if (request.method !== 'PATCH') throw new ApiError(405, 'METHOD_NOT_ALLOWED', 'Метод не поддерживается')
    const patch = await input(request, z.object({ full_name: z.string().trim().min(2).max(160).optional(), role: roleSchema.optional(), stage: stageSchema.nullable().optional() }))
    const role = patch.role || target.role
    const stage = patch.stage !== undefined ? patch.stage : target.stage
    if (userId === user.id && role !== 'ADMIN') throw conflict('CANNOT_REMOVE_OWN_ADMIN', 'Нельзя снять собственные права администратора')
    if (['ADMIN', 'MANAGER'].includes(role) ? stage !== null : stage === null) throw new ApiError(422, 'INVALID_STAGE', 'Проверьте роль и производственный этап')
    const identity = await admin.getUser(target.identity_id)
    await admin.updateUser(target.identity_id, { app_metadata: { ...identity.appMetadata, roles: [role], authorization: { roles: [role] }, stage } })
    return db.transaction(async tx => {
      const [updated] = await tx.update(users).set({ full_name: patch.full_name || target.full_name, role, stage }).where(eq(users.id, userId)).returning()
      await audit(tx, user, 'USER_UPDATED', 'user', userId, requestId, { role, stage })
      return publicUser(updated)
    })
  }
  if (request.method !== 'POST') throw new ApiError(405, 'METHOD_NOT_ALLOWED', 'Метод не поддерживается')
  if (path === '/batches') {
    const data = await input(request, batchInput)
    requireProduction(user, data.stage)
    return db.transaction(async tx => {
      const [created] = await tx.insert(batches).values({ ...data, created_by: user.id }).returning()
      await audit(tx, user, 'BATCH_CREATED', 'batch', created.id, requestId, { quantity: data.initial_quantity, stage: data.stage })
      return { ...created, available_quantity: data.initial_quantity }
    })
  }
  if (path === '/transfers') {
    const data = await input(request, transferInput)
    return db.transaction(async tx => {
      const batch = await getBatch(tx, data.batch_id, true)
      const stage = user.stage || batch.stage
      requireProduction(user, stage)
      const nextStage = stages[stages.indexOf(stage) + 1]
      if (!nextStage) throw new ApiError(422, 'INVALID_STAGE', 'С последнего этапа передача невозможна')
      await requireBalance(tx, batch, stage, data.sent_quantity)
      const [created] = await tx.insert(transfers).values({ ...data, from_stage: stage, to_stage: nextStage, sender_id: user.id, transfer_number: `TR-${new Date().toISOString().slice(0, 10).replaceAll('-', '')}-${randomUUID().slice(0, 8).toUpperCase()}` }).returning()
      await audit(tx, user, 'TRANSFER_CREATED', 'transfer', created.id, requestId, { quantity: data.sent_quantity, from: stage, to: nextStage })
      return created
    })
  }
  const transferRoute = path.match(/^\/transfers\/(\d+)\/(send|receive|confirm|reject)$/)
  if (transferRoute) {
    const transferId = routeId(transferRoute[1]), action = transferRoute[2]
    const receive = action === 'receive' ? await input(request, receiveInput) : null
    const review = ['confirm', 'reject'].includes(action) ? await input(request, reviewInput) : null
    return db.transaction(async tx => {
      const [initial] = await tx.select().from(transfers).where(eq(transfers.id, transferId))
      if (!initial) throw notFound()
      const batch = await getBatch(tx, initial.batch_id, true)
      const [item] = await tx.select().from(transfers).where(eq(transfers.id, transferId)).for('update')
      const now = new Date().toISOString()
      let changes: Partial<typeof transfers.$inferInsert>
      if (action === 'send') {
        requireProduction(user, item.from_stage)
        if (item.sender_id !== user.id || item.status !== 'DRAFT') throw conflict('INVALID_TRANSFER', 'Передача уже отправлена или принадлежит другому отправителю')
        await requireBalance(tx, batch, item.from_stage, item.sent_quantity)
        changes = { status: 'SENT', sent_at: now }
        await tx.update(transfers).set(changes).where(eq(transfers.id, transferId))
        const remaining = await availableQuantity(tx, batch, item.from_stage)
        await tx.update(batches).set({ status: remaining === 0 ? 'TRANSFERRED' : 'IN_PROGRESS', updated_at: now }).where(eq(batches.id, batch.id))
        await notifyStage(tx, item.to_stage, 'Ожидается передача', `Партия ${batch.batch_number}: ${item.sent_quantity} голов`, 'TRANSFER', 'transfer', item.id)
      } else if (action === 'receive') {
        requireProduction(user, item.to_stage)
        if (item.status !== 'SENT') throw conflict('TRANSFER_ALREADY_RECEIVED', 'Передача уже получена или еще не отправлена')
        if (receive!.received_quantity > item.sent_quantity) throw new ApiError(422, 'INVALID_QUANTITY', 'Полученное количество не может превышать отправленное')
        const discrepancy = item.sent_quantity - receive!.received_quantity
        if (discrepancy && !receive!.comment?.trim()) throw new ApiError(422, 'DISCREPANCY_COMMENT_REQUIRED', 'Укажите причину расхождения')
        changes = { received_quantity: receive!.received_quantity, discrepancy, receiver_id: user.id, received_at: now, confirmation_comment: receive!.comment || null, status: discrepancy ? 'DISPUTED' : 'RECEIVED' }
        await tx.update(batches).set({ status: discrepancy ? 'DISPUTED' : 'RECEIVED', updated_at: now }).where(eq(batches.id, batch.id))
        if (discrepancy) await notifyStage(tx, item.to_stage, 'Расхождение требует подтверждения', `${item.transfer_number}: ${discrepancy} голов`, 'DISCREPANCY', 'transfer', item.id)
      } else {
        requireReviewer(user, item.to_stage)
        if (!['RECEIVED', 'DISPUTED'].includes(item.status)) throw conflict('TRANSFER_ALREADY_CONFIRMED', 'Передача еще не получена или уже подтверждена')
        if (action === 'reject') {
          if (!review!.reason?.trim()) throw new ApiError(422, 'REASON_REQUIRED', 'Укажите причину отклонения')
          changes = { status: 'SENT', received_quantity: null, discrepancy: null, receiver_id: null, received_at: null, rejection_reason: review!.reason }
          await tx.update(batches).set({ status: 'TRANSFERRED', updated_at: now }).where(eq(batches.id, batch.id))
        } else {
          if (item.discrepancy && !(review!.reason || item.confirmation_comment)?.trim()) throw new ApiError(422, 'REASON_REQUIRED', 'Укажите причину расхождения')
          changes = { status: 'CONFIRMED', confirmed_by: user.id, confirmed_at: now, confirmation_comment: review!.reason || item.confirmation_comment }
          await tx.update(batches).set({ status: 'IN_PROGRESS', updated_at: now }).where(eq(batches.id, batch.id))
        }
      }
      const [updated] = await tx.update(transfers).set(changes).where(eq(transfers.id, transferId)).returning()
      await audit(tx, user, `TRANSFER_${action === 'send' ? 'SENT' : action === 'receive' ? 'RECEIVED' : action === 'confirm' ? 'CONFIRMED' : 'REJECTED'}`, 'transfer', item.id, requestId, { status: updated.status, received_quantity: updated.received_quantity, discrepancy: updated.discrepancy }, receive?.comment || review?.reason || null)
      return updated
    })
  }
  if (path === '/losses' || path === '/balance-adjustments') {
    const isLoss = path === '/losses'
    const data = isLoss ? await input(request, lossInput) : await input(request, adjustmentInput)
    return db.transaction(async tx => {
      const batch = await getBatch(tx, data.batch_id, true)
      const stage = user.stage || batch.stage
      requireProduction(user, stage)
      await requireBatchVisibility(tx, batch, user)
      if ('quantity' in data) {
        await requireBalance(tx, batch, stage, data.quantity)
        const [created] = await tx.insert(losses).values({ ...data, stage, created_by: user.id, status: data.quantity >= 10 ? 'PENDING' : 'APPROVED', approved_by: data.quantity >= 10 ? null : user.id, approved_at: data.quantity >= 10 ? null : new Date().toISOString() }).returning()
        if (created.status === 'PENDING') await notifyStage(tx, stage, 'Потеря ожидает подтверждения', `Партия ${batch.batch_number}: ${data.quantity} голов`, 'LOSS', 'loss', created.id)
        await audit(tx, user, 'LOSS_CREATED', 'loss', created.id, requestId, { quantity: created.quantity, reason: created.reason })
        return created
      }
      if (data.quantity_delta < 0) await requireBalance(tx, batch, stage, -data.quantity_delta)
      const [created] = await tx.insert(adjustments).values({ ...data, stage, created_by: user.id }).returning()
      await notifyStage(tx, stage, 'Корректировка ожидает подтверждения', `Партия ${batch.batch_number}: ${data.quantity_delta} голов`, 'ADJUSTMENT', 'adjustment', created.id)
      await audit(tx, user, 'BALANCE_CORRECTION_CREATED', 'balance_adjustment', created.id, requestId, { quantity_delta: created.quantity_delta }, created.reason)
      return created
    })
  }
  const reviewRoute = path.match(/^\/(losses|balance-adjustments)\/(\d+)\/(approve|reject)$/)
  if (reviewRoute) {
    const isLoss = reviewRoute[1] === 'losses', recordId = routeId(reviewRoute[2]), approve = reviewRoute[3] === 'approve'
    const review = approve ? null : await input(request, reviewInput)
    return db.transaction(async tx => {
      const [initial] = isLoss ? await tx.select().from(losses).where(eq(losses.id, recordId)) : await tx.select().from(adjustments).where(eq(adjustments.id, recordId))
      if (!initial) throw notFound()
      const batch = await getBatch(tx, initial.batch_id, true)
      const [item] = isLoss ? await tx.select().from(losses).where(eq(losses.id, recordId)).for('update') : await tx.select().from(adjustments).where(eq(adjustments.id, recordId)).for('update')
      requireReviewer(user, item.stage)
      if (item.status !== 'PENDING') throw conflict('ALREADY_REVIEWED', 'Запись уже рассмотрена')
      if (approve && item.created_by === user.id) throw new ApiError(403, 'SEPARATION_OF_DUTIES_REQUIRED', 'Подтверждение должен выполнить другой сотрудник')
      if (!approve && !review?.reason?.trim()) throw new ApiError(422, 'REASON_REQUIRED', 'Укажите причину отклонения')
      const debit = 'quantity' in item ? item.quantity : Math.max(0, -item.quantity_delta)
      if (approve && debit) await requireBalance(tx, batch, item.stage, debit)
      const changes = { status: approve ? 'APPROVED' as const : 'REJECTED' as const, approved_by: user.id, approved_at: new Date().toISOString() }
      const [updated] = isLoss ? await tx.update(losses).set(changes).where(eq(losses.id, recordId)).returning() : await tx.update(adjustments).set(changes).where(eq(adjustments.id, recordId)).returning()
      await audit(tx, user, `${isLoss ? 'LOSS' : 'BALANCE_CORRECTION'}_${approve ? 'APPROVED' : 'REJECTED'}`, isLoss ? 'loss' : 'balance_adjustment', recordId, requestId, { status: updated.status }, review?.reason || null)
      return updated
    })
  }
  throw notFound()
}

export default async (request: Request, context: Context): Promise<Response> => {
  const requestId = context.requestId || randomUUID()
  let response: Response
  try {
    const url = new URL(request.url)
    const path = url.pathname.replace(/^\/api(?=\/|$)/, '')
    if (!['GET', 'POST', 'PATCH'].includes(request.method)) throw new ApiError(405, 'METHOD_NOT_ALLOWED', 'Метод не поддерживается')
    if (request.method !== 'GET') verifyRequestOrigin(request)
    const db = getDb()
    if (path === '/health' && request.method === 'GET') {
      await db.select({ id: users.id }).from(users).limit(1)
      response = Response.json({ status: 'ok', backend: 'netlify', database: 'connected', auth: 'netlify-identity' })
    } else {
      const { user } = await authenticate(db)
      const result = request.method === 'GET' ? await readRoute(db, user, path, url.searchParams) : await writeRoute(db, user, request, path, requestId)
      response = result instanceof Response ? result : Response.json(result, { status: request.method === 'POST' && ['/batches', '/transfers', '/losses', '/balance-adjustments', '/users'].includes(path) ? 201 : 200 })
    }
  } catch (error) {
    let status = 503, code = 'SERVICE_UNAVAILABLE', message = 'Сервис временно недоступен. Проверьте состояние Netlify Database и Identity.'
    if (error instanceof ApiError) { status = error.status; code = error.code; message = error.message }
    else if (error instanceof z.ZodError) { status = 422; code = 'VALIDATION_ERROR'; message = 'Проверьте введенные данные: количество, этап, email и требования к паролю' }
    else if (error instanceof MissingIdentityError) { code = 'IDENTITY_NOT_CONFIGURED'; message = 'Netlify Identity еще не активирован. Завершите настройку Identity в панели проекта.' }
    else if (error instanceof AuthError) { status = error.status === 403 ? 403 : error.status === 401 ? 401 : error.status === 422 ? 409 : 503; code = 'IDENTITY_ERROR'; message = status === 403 ? 'Запрос отклонен. Проверьте права доступа и откройте приложение с его основного адреса.' : status === 401 ? 'Сессия завершена. Войдите снова.' : status === 409 ? 'Учетная запись с таким email уже существует' : 'Не удалось связаться с Netlify Identity' }
    else if ((error as { cause?: { code?: string }; code?: string })?.cause?.code === '23505' || (error as { code?: string })?.code === '23505') { status = 409; code = 'ALREADY_EXISTS'; message = 'Запись с таким номером или email уже существует' }
    response = Response.json({ success: false, error: { code, message }, request_id: requestId }, { status })
  }
  response.headers.set('cache-control', 'no-store')
  response.headers.set('x-content-type-options', 'nosniff')
  response.headers.set('x-request-id', requestId)
  return response
}

export const config: Config = { path: '/api/*' }
