export class ApiError extends Error {
  code: string
  requestId?: string
  status: number
  constructor(message: string, status: number, code = 'request_failed', requestId?: string) {
    super(message); this.name = 'ApiError'; this.status = status; this.code = code; this.requestId = requestId
  }
}

export async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  const body = await response.json().catch(() => ({})) as Record<string, any>
  if (!response.ok) {
    const detail = body.detail && typeof body.detail === 'object' ? body.detail : body
    throw new ApiError(String(detail.message || body.detail || body.message || `请求失败：${response.status}`), response.status,
      String(detail.code || 'request_failed'), response.headers.get('x-request-id') || undefined)
  }
  return body as T
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.requestId ? `（请求 ID：${error.requestId}）` : ''}`
  return error instanceof Error ? error.message : '请求失败'
}
