import { createRef, useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { OcrProofing, ParseModeSelector, Table, TaskCenter, TaskView, ocrPages } from './main'
import type { ParseMode } from './main'

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
  it('shows only available result views and expands completed task events on demand', () => {
    const completed = { ...task, status: 'succeeded', result: { ...task.result, status: 'succeeded', artifacts: [{ artifact_id: 'artifact', filename: 'document.md' }] } }
    const events = [
      { sequence: 1, type: 'created', at: new Date().toISOString(), details: {} },
      { sequence: 2, type: 'planned', at: new Date().toISOString(), details: {} },
      { sequence: 3, type: 'running', at: new Date().toISOString(), details: {} },
      { sequence: 4, type: 'finished', at: new Date().toISOString(), details: {} },
    ]
    render(<TaskView task={completed as any} headingRef={createRef()} events={events} plans={[]} tables={[]} representations={{}} artifacts={completed.result.artifacts} activeTab="overview" setActiveTab={vi.fn()} onCancel={vi.fn()} onRetry={vi.fn()} onDelete={vi.fn()} backToList={vi.fn()} />)
    expect(screen.queryByRole('tab', { name: 'HTML 预览' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开全部 4 个事件' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开全部 4 个事件' }))
    expect(screen.getByRole('button', { name: '收起事件' })).toBeInTheDocument()
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

describe('OCR proofing', () => {
  it('anchors coordinate overlays to the source image stage', () => {
    const document = { document_type: 'image', source_file: { file_id: `file_${'4'.repeat(32)}`, filename: 'label.png' }, metadata: { width: 1000, height: 500 }, blocks: [{ text: 'BODUO', bbox: [100, 50, 500, 150], metadata: { confidence: .99 } }] }
    render(<OcrProofing document={document as any} taskId={`task_${'5'.repeat(32)}`} />)
    const stage = screen.getByRole('img', { name: 'label.png OCR 校对' }).parentElement
    expect(stage).toHaveClass('ocr-image-stage')
    expect(stage).toHaveStyle({ aspectRatio: '1000 / 500' })
    expect(screen.getByRole('button', { name: '文本块 1：BODUO' })).toHaveStyle({ left: '10%', top: '10%', width: '40%', height: '20%' })
  })

  it('renders scanned PDF results that carry page-level dimensions', () => {
    // Scanned PDFs are document_type='pdf' and keep width/height on each page,
    // not on document.metadata; proofing must still work.
    const document = { document_type: 'pdf', source_file: { file_id: `file_${'6'.repeat(32)}`, filename: 'scan.pdf' },
      pages: [{ page_number: 1, width: 200, height: 100, preview: { filename: 'ocr-page-0001.png' }, blocks: [{ text: '第一页', bbox: [0, 0, 100, 20] }] }] }
    render(<OcrProofing document={document as any} taskId={`task_${'7'.repeat(32)}`} />)
    const stage = screen.getByRole('img', { name: 'scan.pdf OCR 校对' }).parentElement
    expect(stage).toHaveStyle({ aspectRatio: '200 / 100' })
    expect(screen.getByRole('button', { name: '文本块 1：第一页' })).toHaveStyle({ left: '0%', top: '0%', width: '50%', height: '20%' })
  })

  it('switches pages and resets the selected block', () => {
    const document = { document_type: 'pdf', source_file: { file_id: `file_${'8'.repeat(32)}`, filename: 'scan.pdf' },
      pages: [
        { page_number: 1, width: 200, height: 100, preview: { filename: 'ocr-page-0001.png' }, blocks: [{ text: '第一页', bbox: [0, 0, 100, 20], metadata: { confidence: .9 } }] },
        { page_number: 2, width: 200, height: 100, preview: { filename: 'ocr-page-0002.png' }, blocks: [{ text: '第二页', bbox: [0, 0, 100, 20], metadata: { confidence: .8 } }] },
      ] }
    render(<OcrProofing document={document as any} taskId={`task_${'9'.repeat(32)}`} />)
    fireEvent.click(screen.getByRole('button', { name: '文本块 1：第一页' }))
    expect(screen.getByText('文本块 1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '第 2 页' }))
    expect(screen.getByRole('button', { name: '文本块 1：第二页' })).toBeInTheDocument()
    // Selecting a new page clears the previous selection panel.
    expect(screen.queryByText('置信度：0.900')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '第 2 页' })).toHaveAttribute('aria-current', 'page')
  })

  it('prefers the published preview artifact over the source file', () => {
    const document = { document_type: 'pdf', source_file: { file_id: `file_${'a'.repeat(32)}`, filename: 'scan.pdf' },
      pages: [{ page_number: 1, width: 200, height: 100, preview: { filename: 'ocr-page-0001.png' }, blocks: [{ text: '第一页', bbox: [0, 0, 100, 20] }] }] }
    const artifacts = [{ artifact_id: `artifact_${'b'.repeat(32)}`, filename: 'ocr-page-0001.png', content_type: 'image/png', download_url: '/preview.png' }]
    render(<OcrProofing document={document as any} taskId={`task_${'c'.repeat(32)}`} artifacts={artifacts as any} />)
    expect(screen.getByRole('img', { name: 'scan.pdf OCR 校对' })).toHaveAttribute('src', '/preview.png')
  })

  it('explains when a page has no usable preview instead of rendering a blank pane', () => {
    const document = { document_type: 'pdf', source_file: { filename: 'scan.pdf' },
      pages: [{ page_number: 1, width: 200, height: 100, blocks: [{ text: '第一页', bbox: [0, 0, 100, 20] }] }] }
    render(<OcrProofing document={document as any} taskId={`task_${'d'.repeat(32)}`} />)
    expect(screen.getByText('该页没有可用的预览图，仅显示识别版面。')).toBeInTheDocument()
  })
})

describe('ocrPages', () => {
  const block = { text: 'x', bbox: [0, 0, 10, 10] }

  it('ignores blocks without usable bounding boxes', () => {
    const document = { document_type: 'image', metadata: { width: 10, height: 10 },
      pages: [{ page_number: 1, blocks: [block, { text: 'no bbox' }, { text: 'short', bbox: [1, 2] }] }] }
    const pages = ocrPages(document as any)
    expect(pages).toHaveLength(1)
    expect(pages[0].blocks).toEqual([block])
  })

  it('drops pages that carry no OCR coordinates', () => {
    const document = { document_type: 'pdf', metadata: { width: 5, height: 5 },
      pages: [{ page_number: 1, blocks: [] }, { page_number: 2, blocks: [block] }] }
    const pages = ocrPages(document as any)
    expect(pages.map((page) => page.pageNumber)).toEqual([2])
  })

  it('falls back to document-level blocks when no page carries coordinates', () => {
    const document = { document_type: 'image', metadata: { width: 10, height: 20 }, blocks: [block] }
    const pages = ocrPages(document as any)
    expect(pages).toHaveLength(1)
    expect(pages[0]).toMatchObject({ pageNumber: 1, width: 10, height: 20 })
  })

  it('returns nothing when the result has no OCR coordinates at all', () => {
    expect(ocrPages({ document_type: 'pdf', pages: [{ page_number: 1, blocks: [{ text: 'plain' }] }] } as any)).toEqual([])
    expect(ocrPages(undefined)).toEqual([])
  })
})

describe('Parse mode selector', () => {
  function Harness({ enhancedAvailable = true }: { enhancedAvailable?: boolean }) {
    const [mode, setMode] = useState<ParseMode>('auto')
    const [open, setOpen] = useState(false)
    return <ParseModeSelector value={mode} onChange={setMode} open={open} setOpen={setOpen} enhancedAvailable={enhancedAvailable} />
  }

  it('opens with the keyboard and moves focus with arrows, Home and End', () => {
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: /智能默认/ })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(3)
    expect(options[0]).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowDown' })
    expect(options[1]).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'End' })
    expect(options[2]).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Home' })
    expect(options[0]).toHaveFocus()
  })

  it('closes on Escape and returns focus to the trigger', () => {
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: /智能默认/ })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('selects an option and marks unavailable modes as disabled', () => {
    render(<Harness enhancedAvailable={false} />)
    fireEvent.click(screen.getByRole('button', { name: /智能默认/ }))
    expect(screen.getByRole('option', { name: /增强解析/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('option', { name: /标准解析/ }))
    expect(screen.getByRole('button', { name: /标准解析/ })).toHaveAttribute('aria-expanded', 'false')
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
