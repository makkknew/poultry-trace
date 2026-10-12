import { admin, getUser } from '@netlify/identity'
import type { User as IdentityUser } from '@netlify/identity'
import { eq } from 'drizzle-orm'
import { users, roles, stages } from '../../db/schema.js'
import type { Database } from '../../db/index.js'
import { ApiError, forbidden } from './errors.js'

export type AppUser = typeof users.$inferSelect
export type Stage = typeof stages[number]
export type Role = typeof roles[number]

export function trustedAssignment(identity: IdentityUser): { role: Role; stage: Stage | null } {
  const metadata = identity.appMetadata || {}
  const authorization = metadata.authorization as { roles?: unknown } | undefined
  const candidates = identity.roles || metadata.roles || authorization?.roles || []
  const assigned = Array.isArray(candidates) ? candidates.map(value => String(value).toUpperCase()) : []
  const role = [...roles].reverse().find(value => assigned.includes(value))
  if (!role) throw new ApiError(403, 'ACCOUNT_NOT_ASSIGNED', 'Учетная запись подтверждена, но доступ не назначен. Администратор должен назначить роль в Netlify Identity.')
  const stage = metadata.stage
  if (role === 'ADMIN' || role === 'MANAGER') return { role, stage: null }
  if (!stages.includes(stage as Stage)) throw new ApiError(403, 'STAGE_NOT_ASSIGNED', 'Администратор должен назначить производственный этап учетной записи.')
  return { role, stage: stage as Stage }
}

export async function authenticate(db: Database) {
  const session = await getUser()
  if (!session) throw new ApiError(401, 'AUTH_REQUIRED', 'Войдите в систему или восстановите пароль')
  const identity = await admin.getUser(session.id)
  const assignment = trustedAssignment(identity)
  const [existing] = await db.select().from(users).where(eq(users.identity_id, identity.id))
  if (existing && !existing.is_active) throw new ApiError(403, 'ACCOUNT_DISABLED', 'Доступ к учетной записи отключен администратором')
  const email = identity.email?.trim().toLowerCase()
  if (!email) throw forbidden()
  const [user] = await db.insert(users).values({
    identity_id: identity.id,
    username: email,
    full_name: existing?.full_name || String(identity.name || identity.userMetadata?.full_name || email).slice(0, 160),
    ...assignment,
  }).onConflictDoUpdate({
    target: users.identity_id,
    set: { username: email, ...assignment },
  }).returning()
  return { user, identity }
}

export function requireRole(user: AppUser, allowed: readonly Role[]) {
  if (!allowed.includes(user.role)) throw forbidden()
}

export function requireStage(user: AppUser, stage: Stage) {
  if (!['ADMIN', 'MANAGER'].includes(user.role) && user.stage !== stage) throw forbidden()
}

export function requireProduction(user: AppUser, stage: Stage) {
  requireRole(user, ['OPERATOR', 'SHIFT_LEAD', 'ADMIN'])
  requireStage(user, stage)
}

export function requireReviewer(user: AppUser, stage: Stage) {
  requireRole(user, ['SHIFT_LEAD', 'ADMIN'])
  requireStage(user, stage)
}

export function publicUser(user: AppUser) {
  const { identity_id, ...profile } = user
  return profile
}
