import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Table, TaskCenter, TaskView } from './main'

const task = { task_id: `task_${'1'.repeat(32)}`, file_id: `file_${'2'.repeat(32)}`, status: 'interrupted', created_at: new Date().toISOString(), result: { status: 'failed', artifacts: [], steps: [], warnings: [], metrics: {} } }

describe('TaskView accessibility', () => {
  it('supports End key tab navigation and explicit interrupted status', () => {
    const setActiveTab = vi.fn()
    render(<TaskView task={task as any} headingRef={createRef()} events={[]} plans={[]} tables={[]} representations={{}} artifacts={[]} activeTab="overview" setActiveTab={setActiveTab} onCancel={vi.fn()} onRetry={vi.fn()} onDelete={vi.fn()} backToList={vi.fn()} />)
    fireEvent.keyDown(screen.getByRole('tablist'), { key: 'End' })
    expect(setActiveTab).toHaveBeenCalledWith('details')
    expect(screen.getByRole('tab', { name: '执行详情' })).toHaveFocus()
    expect(screen.getByText('任务被服务重启中断')).toBeInTheDocument()
  })
  it('lazy-loads image artifact previews', () => {
    const artifact = { artifact_id: `artifact_${'3'.repeat(32)}`, filename: 'page.png', content_type: 'image/png', download_url: '/image.png' }
    render(<TaskView task={task as any} headingRef={createRef()} events={[]} plans={[]} tables={[]} representations={{}} artifacts={[artifact]} activeTab="artifacts" setActiveTab={vi.fn()} onCancel={vi.fn()} onRetry={vi.fn()} onDelete={vi.fn()} backToList={vi.fn()} />)
    expect(screen.getByRole('img', { name: 'page.png 预览' })).toHaveAttribute('loading', 'lazy')
    expect(screen.getByRole('link', { name: '下载' })).toHaveAttribute('href', '/image.png')
  })
})

describe('TaskCenter', () => {
  it('updates filters and exposes cursor pagination', () => {
    const setFilters = vi.fn(); const loadMore = vi.fn(); const filters = { status: '', fileType: '', provider: '', errorCode: '', query: '', createdAfter: '', createdBefore: '' }
    render(<TaskCenter recent={[task as any]} open={vi.fn()} filters={filters} setFilters={setFilters} nextCursor="next" loadMore={loadMore} />)
    fireEvent.change(screen.getByLabelText('状态'), { target: { value: 'interrupted' } })
    expect(setFilters).toHaveBeenCalledWith({ ...filters, status: 'interrupted' })
    fireEvent.click(screen.getByRole('button', { name: '加载更多' })); expect(loadMore).toHaveBeenCalled()
  })
})

describe('Table', () => {
  it('paginates rows with an accessible caption', () => {
    const rows = [['name'], ...Array.from({ length: 101 }, (_, index) => [`row-${index + 1}`])]
    render(<Table table={{ name: 'Large', rows }} />)
    expect(screen.getByRole('table', { name: /第 1 页，共 3 页/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByText('row-51')).toBeInTheDocument()
  })
})
