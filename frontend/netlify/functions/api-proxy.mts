/**
 * Same-origin bridge from the Netlify site to the separately hosted FastAPI.
 * Configure API_ORIGIN in Netlify's site environment (for example https://api.example.com).
 * Keeping API requests same-origin lets the existing HttpOnly refresh cookie work on netlify.app.
 */
export default async (request: Request): Promise<Response> => {
  const apiOrigin = process.env.API_ORIGIN
  if (!apiOrigin) {
    return Response.json({ error: { message: 'API_ORIGIN is not configured for this Netlify site' } }, { status: 503 })
  }

  const incoming = new URL(request.url)
  const path = incoming.pathname.replace(/^\/api(?=\/|$)/, '') || '/'
  const target = new URL(`${path}${incoming.search}`, apiOrigin)
  const headers = new Headers(request.headers)
  for (const name of ['host', 'connection', 'content-length', 'transfer-encoding', 'origin', 'referer']) {
    headers.delete(name)
  }

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect: 'manual',
    })
    const responseHeaders = new Headers()
    upstream.headers.forEach((value, key) => {
      if (!['connection', 'content-length', 'transfer-encoding'].includes(key.toLowerCase()) && key.toLowerCase() !== 'set-cookie') {
        responseHeaders.set(key, value)
      }
    })
    for (const cookie of upstream.headers.getSetCookie?.() ?? []) responseHeaders.append('set-cookie', cookie)
    responseHeaders.set('cache-control', 'no-store')
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders })
  } catch {
    return Response.json({ error: { message: 'Не удалось подключиться к API Poultry Trace' } }, { status: 502 })
  }
}

export const config = { path: '/api/*' }
