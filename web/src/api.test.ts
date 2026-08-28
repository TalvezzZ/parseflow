import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, errorMessage } from './api'

describe('api', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('normalizes error code and request id', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'task_not_found', message: '未找到任务' } }), { status: 404, headers: { 'content-type': 'application/json', 'x-request-id': 'req-123' } })))
    await expect(api('/api/v1/tasks/missing')).rejects.toMatchObject({ code: 'task_not_found', requestId: 'req-123', status: 404 })
    expect(errorMessage(new ApiError('未找到任务', 404, 'task_not_found', 'req-123'))).toContain('req-123')
  })
})
