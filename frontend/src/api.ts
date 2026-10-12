let accessToken: string | null = null
export const setAccessToken = (token: string | null) => { accessToken = token }

export async function api<T = any>(path: string, options: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  let response = await fetch(`/api${path}`, { ...options, headers, credentials: 'include' })
  if (response.status === 401 && retry && path !== '/auth/login' && path !== '/auth/refresh') {
    const refreshed = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' })
    if (refreshed.ok) {
      const data = await refreshed.json(); accessToken = data.access_token
      headers.set('Authorization', `Bearer ${accessToken}`)
      response = await fetch(`/api${path}`, { ...options, headers, credentials: 'include' })
    }
  }
  const contentType = response.headers.get('content-type') || ''
  const data = contentType.includes('application/json') ? await response.json() : await response.blob()
  if (!response.ok) throw new Error(data?.error?.message || data?.detail || `Ошибка ${response.status}`)
  return data as T
}

export async function download(path: string, filename: string) {
  const headers = new Headers()
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  let response = await fetch(`/api${path}`, { headers, credentials: 'include' })
  if (response.status === 401) {
    const refreshed = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' })
    if (refreshed.ok) { const d=await refreshed.json(); accessToken=d.access_token; headers.set('Authorization',`Bearer ${accessToken}`); response=await fetch(`/api${path}`,{headers,credentials:'include'}) }
  }
  if (!response.ok) throw new Error('Не удалось скачать отчет')
  const url=URL.createObjectURL(await response.blob()); const a=document.createElement('a');a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url)
}
