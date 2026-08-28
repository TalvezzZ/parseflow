import { ChangeEvent, DragEvent, FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import './dashboard.css'
import { ApiError, api, errorMessage } from './api'

type Json = Record<string, unknown>
type TaskStatus = 'queued' | 'planning' | 'running' | 'cancelling' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
type Artifact = { artifact_id: string; filename: string; content_type?: string; size_bytes?: number; kind?: string; download_url?: string }
type TaskResult = { status: 'succeeded' | 'partial' | 'failed'; document?: Json; artifacts: Artifact[]; steps: Json[]; conversion?: Json; warnings: string[]; metrics: Json; quality?: Json; provenance?: Json; error?: Json }
type Task = { task_id: string; file_id: string; filename?: string; content_type?: string; status: TaskStatus; data_id?: string; goal?: string; retry_of?: string; created_at: string; updated_at?: string; started_at?: string; finished_at?: string; duration_ms?: number; error?: Json; plan?: Json; result?: TaskResult; warnings?: string[] }
type TaskEvent = { sequence: number; type: string; at: string; step?: string; message?: string; details: Json }
type SubmittedTask = { task_id: string; file_id: string; status: 'queued'; created_at: string }
type Capabilities = { allowed_suffixes: string[]; max_upload_bytes: number; max_result_bytes: number; task_filters: string[] }
type Filters = { status: string; fileType: string; provider: string; errorCode: string; query: string; createdAfter: string; createdBefore: string }
const emptyFilters: Filters = { status: '', fileType: '', provider: '', errorCode: '', query: '', createdAfter: '', createdBefore: '' }
const terminal = new Set<TaskStatus>(['succeeded', 'partial', 'failed', 'cancelled', 'interrupted'])

const statusLabels: Record<TaskStatus, string> = { queued: '等待处理', planning: '正在制定解析方案', running: '正在执行解析', cancelling: '正在取消', succeeded: '解析完成', partial: '部分完成', failed: '解析失败', cancelled: '已取消', interrupted: '服务中断' }

function upload(form: FormData, progress: (value: number) => void, register: (xhr: XMLHttpRequest) => void): Promise<SubmittedTask> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); register(xhr); xhr.open('POST', '/api/v1/tasks/parse'); xhr.responseType = 'json'
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)) }
    xhr.onload = () => { const detail = xhr.response?.detail || {}; xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response as SubmittedTask) : reject(new ApiError(detail.message || `上传失败：${xhr.status}`, xhr.status, detail.code || 'upload_failed', xhr.getResponseHeader('x-request-id') || undefined)) }
    xhr.onerror = () => reject(new Error('上传网络连接失败')); xhr.onabort = () => reject(new Error('上传已取消')); xhr.send(form)
  })
}
function taskLoadError(error: unknown) { return error instanceof ApiError && error.status === 404 ? `任务已删除或过期，无法恢复。${error.requestId ? `（请求 ID：${error.requestId}）` : ''}` : errorMessage(error) }
function documentOf(task?: Task | null): Json | undefined { return task?.result?.document }
function artifactsOf(task?: Task | null): Artifact[] { return task?.result?.artifacts || [] }
function representationsOf(document?: Json): Json {
  const raw = document?.representations
  const representations = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Json : {}
  const pageText = Array.isArray(document?.pages) ? document.pages
    .filter((page): page is Json => Boolean(page) && typeof page === 'object' && !Array.isArray(page))
    .map((page) => String(page.text || ''))
    .filter(Boolean)
    .join('\n\n') : ''
  return { ...representations, plain_text: representations.plain_text || pageText, markdown: representations.markdown || pageText }
}
function formatDate(value?: string) { return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value)) : '—' }
function fileSize(bytes: number) { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB` }
function formatDuration(milliseconds?: number) {
  if (milliseconds === undefined || !Number.isFinite(milliseconds) || milliseconds < 0) return '—'
  const seconds = Math.floor(milliseconds / 1000)
  if (seconds < 60) return `${seconds}.${Math.floor((milliseconds % 1000) / 100)} 秒`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${seconds % 60} 秒`
}
function taskDuration(task: Task) {
  if (task.duration_ms !== undefined) return formatDuration(task.duration_ms)
  const createdAt = Date.parse(task.created_at)
  return Number.isNaN(createdAt) ? '—' : formatDuration(Date.now() - createdAt)
}
function taskFilename(task: Task) { const source = task.result?.document?.source_file as Json | undefined; return String(task.filename || source?.filename || task.data_id || task.task_id) }
function filterUrl(filters: Filters) { const params = new URLSearchParams(); const entries: [string,string][] = [['status',filters.status],['file_type',filters.fileType],['provider',filters.provider],['error_code',filters.errorCode],['query',filters.query],['created_after',filters.createdAfter],['created_before',filters.createdBefore]]; entries.forEach(([key,value]) => { if (value) params.set(key,value) }); return params.toString() ? `/?${params}` : '/' }
function taskQuery(filters: Filters, cursor?: string) {
  const params = new URLSearchParams({ limit: '20', sort: 'created_desc' })
  const iso = (value: string) => value ? new Date(value).toISOString() : ''
  const entries: [string, string][] = [['status', filters.status], ['file_type', filters.fileType], ['provider', filters.provider], ['error_code', filters.errorCode], ['query', filters.query], ['created_after', iso(filters.createdAfter)], ['created_before', iso(filters.createdBefore)]]
  entries.forEach(([key, value]) => { if (value) params.set(key, value) }); if (cursor) params.set('cursor', cursor)
  return `/api/v1/tasks?${params}`
}

export function App() {
  const [file, setFile] = useState<File | null>(null)
  const [goal, setGoal] = useState('')
  const [task, setTask] = useState<Task | null>(null)
  const [recent, setRecent] = useState<Task[]>([])
  const [activeTab, setActiveTab] = useState<'overview' | 'text' | 'tables' | 'markdown' | 'html' | 'json' | 'artifacts' | 'details'>('overview')
  const [error, setError] = useState('')
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [stale, setStale] = useState(false)
  const [filters, setFilters] = useState<Filters>(() => { const p = new URLSearchParams(window.location.search); return { status: p.get('status') || '', fileType: p.get('file_type') || '', provider: p.get('provider') || '', errorCode: p.get('error_code') || '', query: p.get('query') || '', createdAfter: p.get('created_after') || '', createdBefore: p.get('created_before') || '' } })
  const [nextCursor, setNextCursor] = useState<string | undefined>()
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const uploadRef = useRef<XMLHttpRequest | null>(null)
  const errorRef = useRef<HTMLParagraphElement>(null)
  const listRequestRef = useRef(0)
  const pollInFlightRef = useRef(false)
  useEffect(() => { if (error) errorRef.current?.focus() }, [error])

  useEffect(() => {
    api<Capabilities>('/api/v1/capabilities').then(setCapabilities).catch(() => undefined)
    const match = window.location.pathname.match(/^\/tasks\/(task_[0-9a-f]{32})$/)
    if (match) api<Task>(`/api/v1/tasks/${match[1]}`).then((item) => { setTask(item); setTimeout(() => headingRef.current?.focus(), 0) }).catch((err) => setError(taskLoadError(err)))
  }, [])

  useEffect(() => { const pop = () => { const match = window.location.pathname.match(/^\/tasks\/(task_[0-9a-f]{32})$/); if (!match) { setTask(null); return } api<Task>(`/api/v1/tasks/${match[1]}`).then(setTask).catch((err) => setError(taskLoadError(err))) }; window.addEventListener('popstate', pop); return () => window.removeEventListener('popstate', pop) }, [])

  useEffect(() => {
    if (window.location.pathname === '/') window.history.replaceState({}, '', filterUrl(filters))
    const generation = ++listRequestRef.current
    const id = window.setTimeout(() => api<{ items: Task[]; next_cursor?: string }>(taskQuery(filters))
      .then((payload) => { if (generation === listRequestRef.current) { setRecent(payload.items); setNextCursor(payload.next_cursor); setStale(false) } })
      .catch((err) => { if (generation === listRequestRef.current) { setError(errorMessage(err)); setStale(true) } }), 200)
    return () => window.clearTimeout(id)
  }, [filters])

  useEffect(() => { if (task) headingRef.current?.focus() }, [task?.task_id])

  useEffect(() => {
    if (!task || terminal.has(task.status)) return
    let stopped = false; let timer = 0
    const refresh = async () => {
      if (pollInFlightRef.current) return
      pollInFlightRef.current = true
      try { const next = await api<Task>(`/api/v1/tasks/${task.task_id}`); if (!stopped) { setTask(next); setRecent((items) => items.map((item) => item.task_id === next.task_id ? next : item)); setStale(false) } }
      catch (err) { if (!stopped) { setError(errorMessage(err)); setStale(true) } }
      finally { pollInFlightRef.current = false; const age = Date.now() - Date.parse(task.created_at); const delay = globalThis.document.hidden ? 10000 : age > 120000 ? 5000 : 1200; if (!stopped) timer = window.setTimeout(refresh, delay) }
    }
    const visible = () => { if (!globalThis.document.hidden) { window.clearTimeout(timer); refresh() } }
    timer = window.setTimeout(refresh, 500); globalThis.document.addEventListener('visibilitychange', visible)
    return () => { stopped = true; window.clearTimeout(timer); globalThis.document.removeEventListener('visibilitychange', visible) }
  }, [task?.task_id, task?.status])

  useEffect(() => {
    if (!task) { setEvents([]); return }
    let stopped = false; let timer = 0
    const refresh = async () => { try { const payload = await api<{ items: TaskEvent[] }>(`/api/v1/tasks/${task.task_id}/events?limit=500`); if (!stopped) setEvents((old) => payload.items.length >= old.length ? payload.items : old) } catch { /* task polling reports network state */ } finally { if (!stopped && !terminal.has(task.status)) timer = window.setTimeout(refresh, globalThis.document.hidden ? 5000 : 1500) } }
    refresh()
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [task?.task_id, task?.status])

  const openTask = (item: Task) => { setTask(item); setActiveTab('overview'); setError(''); window.history.pushState({}, '', `/tasks/${item.task_id}`); window.setTimeout(() => headingRef.current?.focus(), 0) }
  const retryTask = async () => { if (!task) return; try { const created = await api<SubmittedTask>(`/api/v1/tasks/${task.task_id}/retry`, { method: 'POST' }); const next = await api<Task>(`/api/v1/tasks/${created.task_id}`); setRecent((items) => [next, ...items.filter((item) => item.task_id !== next.task_id)]); openTask(next) } catch (err) { setError(errorMessage(err)) } }
  const cancelTask = async () => { if (!task) return; try { setTask(await api<Task>(`/api/v1/tasks/${task.task_id}/cancel`, { method: 'POST' })) } catch (err) { setError(errorMessage(err)) } }
  const deleteTask = async () => { if (!task) return; try { await api<void>(`/api/v1/tasks/${task.task_id}`, { method: 'DELETE' }); setTask(null); setEvents([]); window.history.pushState({}, '', filterUrl(filters)); setRecent((items) => items.filter((item) => item.task_id !== task.task_id)) } catch (err) { setError(errorMessage(err)) } }

  const document = useMemo(() => documentOf(task), [task])
  const choose = (candidate?: File) => { if (!candidate) return; const suffix = `.${candidate.name.split('.').pop()?.toLowerCase()}`; if (capabilities && (!capabilities.allowed_suffixes.includes(suffix) || candidate.size > capabilities.max_upload_bytes)) { setError(candidate.size > capabilities.max_upload_bytes ? `文件超过 ${fileSize(capabilities.max_upload_bytes)} 上限` : `不支持 ${suffix} 文件`); return } setFile(candidate); setError('') }
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!file) return setError('请先选择一个文件')
    setSubmitting(true); setUploadProgress(0); setError('')
    try {
      const form = new FormData(); form.append('file', file); if (goal.trim()) form.append('goal', goal.trim())
      const created = await upload(form, setUploadProgress, (xhr) => { uploadRef.current = xhr })
      const createdTask = await api<Task>(`/api/v1/tasks/${created.task_id}`)
      openTask(createdTask); setRecent((items) => [createdTask, ...items.filter((item) => item.task_id !== createdTask.task_id)])
    } catch (err) { setError(errorMessage(err)) } finally { setSubmitting(false); setUploadProgress(0); uploadRef.current = null }
  }
  const onDrop = (event: DragEvent<HTMLDivElement>) => { event.preventDefault(); choose(event.dataTransfer.files[0]) }
  const openFilePicker = () => inputRef.current?.click()
  const onDropzoneKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openFilePicker() }
  }
  const plans = (task?.plan?.steps as Json[] | undefined) || []
  const tables = (document?.tables as Json[] | undefined) || []
  const representations = representationsOf(document)
  const artifacts = useMemo(() => artifactsOf(task), [task])
  const fileId = task?.file_id
  const loadMore = async () => { if (!nextCursor) return; const generation = listRequestRef.current; try { const payload = await api<{ items: Task[]; next_cursor?: string }>(taskQuery(filters, nextCursor)); if (generation === listRequestRef.current) { setRecent((items) => [...items, ...payload.items.filter((item) => !items.some((existing) => existing.task_id === item.task_id))]); setNextCursor(payload.next_cursor) } } catch (err) { if (generation === listRequestRef.current) { setError(errorMessage(err)); setStale(true) } } }

  return <main className="app-shell">
    <header className="app-header"><span className="brand-mark" aria-hidden="true">✦</span><div><p className="eyebrow">PARSEFLOW · 1.3.0</p><h1>智能文档工作台</h1><p className="subtitle">上传一个文件，系统会自动规划并执行合适的解析流程。</p></div><span className={`service ${stale ? 'stale' : ''}`} role="status"><i /> {stale ? '连接中断 · 数据可能已过期' : '服务就绪'}</span></header>
    <section className="workspace-grid">
      <aside className="upload-panel"><h2>开始解析</h2><form onSubmit={submit}>
        <div className="dropzone" role="button" tabIndex={0} aria-label="选择要解析的文件" onDrop={onDrop} onDragOver={(event) => event.preventDefault()} onClick={openFilePicker} onKeyDown={onDropzoneKeyDown}>
          <input ref={inputRef} type="file" onChange={(event: ChangeEvent<HTMLInputElement>) => choose(event.target.files?.[0])} hidden />
          <span className="upload-icon">↑</span><strong>{file ? file.name : '拖拽文件到这里'}</strong><small>{file ? `${file.type || '未知类型'} · ${fileSize(file.size)}` : capabilities ? `支持 ${capabilities.allowed_suffixes.join(' · ')}，最大 ${fileSize(capabilities.max_upload_bytes)}` : '正在读取服务端上传能力…'}</small>
        </div>
        <SupportedFormats />
        <label>解析目标 <span>可选</span><textarea value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：提取表格并输出 Markdown" rows={3} /></label>
        {error && <p className="error" role="alert" tabIndex={-1} ref={errorRef}>{error}</p>}
        {submitting && <div className="upload-progress" role="status" aria-live="polite"><progress max="100" value={uploadProgress} /><span>上传 {uploadProgress}%</span><button type="button" onClick={() => uploadRef.current?.abort()}>取消上传</button></div>}
        <button className="primary" disabled={submitting}>{submitting ? '正在创建任务…' : '开始智能解析'} <span>→</span></button>
      </form>
      <div className="hint"><strong>自动规划</strong><p>系统根据文件类型选择已注册的解析能力；复杂 RTF、旧版 Office 等会自动走增强处理路径。</p></div></aside>
      <section className="content-panel">{task ? <TaskView task={task} headingRef={headingRef} events={events} document={document} fileId={fileId} plans={plans} tables={tables} representations={representations} artifacts={artifacts} activeTab={activeTab} setActiveTab={setActiveTab} onCancel={cancelTask} onRetry={retryTask} onDelete={deleteTask} backToList={() => { setTask(null); setActiveTab('overview'); setError(''); window.history.pushState({}, '', filterUrl(filters)) }} /> : <TaskCenter recent={recent} open={openTask} filters={filters} setFilters={setFilters} nextCursor={nextCursor} loadMore={loadMore} />}</section>
    </section>
  </main>
}

function SupportedFormats() {
  const formats = [
    ['文档', 'PDF · DOC · DOCX · RTF'],
    ['表格', 'XLS · XLSX · XLSM · CSV · TSV'],
    ['演示', 'PPT · PPTX'],
    ['文本与数据', 'TXT · MD · MARKDOWN · HTML · HTM · XML · JSON · YAML · YML · INI · CFG · CONF · LOG · SQL · JS · TS · CSS'],
    ['图片 OCR', 'PNG · JPG · JPEG · WEBP · BMP · TIF · TIFF'],
    ['音频', 'MP3 · WAV · M4A · AAC · FLAC · OGG'],
    ['视频', 'MP4 · MOV · MKV · AVI · WEBM'],
  ]
  return <section className="formats-guide" aria-labelledby="formats-title"><div className="formats-heading"><div><p className="formats-kicker">SUPPORTED FORMATS</p><h3 id="formats-title">支持的文件格式</h3></div><span>40+ 种</span></div><div className="format-list">{formats.map(([category, extensions]) => <div className="format-item" key={category}><strong>{category}</strong><span>{extensions}</span></div>)}</div><p className="formats-note">系统会根据文件类型自动选择合适的解析或预处理流程。</p></section>
}
export function TaskCenter({ recent, open, filters, setFilters, nextCursor, loadMore }: { recent: Task[]; open: (task: Task) => void; filters: Filters; setFilters: (filters: Filters) => void; nextCursor?: string; loadMore: () => void }) {
 const update = (key: keyof Filters, value: string) => setFilters({ ...filters, [key]: value })
 return <div className="task-center"><div className="task-center-heading"><div><p className="eyebrow">TASK CENTER</p><h2>任务中心</h2></div><button type="button" onClick={() => setFilters(emptyFilters)}>清除筛选</button></div><div className="task-filters"><label>搜索<input value={filters.query} onChange={(e) => update('query', e.target.value)} placeholder="文件名、任务或业务 ID" /></label><label>状态<select value={filters.status} onChange={(e) => update('status', e.target.value)}><option value="">全部</option>{Object.entries(statusLabels).map(([value,label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>文件类型<input value={filters.fileType} onChange={(e) => update('fileType', e.target.value)} placeholder="pdf" /></label><label>Provider<input value={filters.provider} onChange={(e) => update('provider', e.target.value)} /></label><label>错误码<input value={filters.errorCode} onChange={(e) => update('errorCode', e.target.value)} /></label><label>开始日期<input type="datetime-local" value={filters.createdAfter} onChange={(e) => update('createdAfter', e.target.value)} /></label><label>结束日期<input type="datetime-local" value={filters.createdBefore} onChange={(e) => update('createdBefore', e.target.value)} /></label></div>{recent.length ? <div className="recent"><h3>任务历史</h3>{recent.map((item) => <button key={item.task_id} onClick={() => open(item)}><span><strong>{taskFilename(item)}</strong><small>{formatDate(item.created_at)} · {item.task_id.slice(0, 13)}…</small></span><em className={`badge ${item.status}`}>{statusLabels[item.status]}</em></button>)}{nextCursor && <button className="load-more" type="button" onClick={loadMore}>加载更多</button>}</div> : <div className="empty"><div className="empty-mark">✦</div><h3>没有符合条件的任务</h3><p>调整筛选条件或上传新文件。</p></div>}</div>
}

export function TaskView({ task, headingRef, events, document, fileId, plans, tables, representations, artifacts, activeTab, setActiveTab, onCancel, onRetry, onDelete, backToList }: { task: Task; headingRef: { current: HTMLHeadingElement | null }; events: TaskEvent[]; document?: Json; fileId?: string; plans: Json[]; tables: Json[]; representations: Json; artifacts: Artifact[]; activeTab: string; setActiveTab: (tab: never) => void; onCancel: () => void; onRetry: () => void; onDelete: () => void; backToList: () => void }) {
  const tabs = [['overview','概览'],['text','正文'],['tables',`表格 ${tables.length || ''}`],['markdown','Markdown'],['html','HTML 预览'],['json','结构化 JSON'],['artifacts',`产物 ${artifacts.length || ''}`],['details','执行详情']] as const
  const duration = taskDuration(task)
  const durationLabel = terminal.has(task.status) ? '整体执行时间' : '已用时间'
  const onTabKey = (event: KeyboardEvent<HTMLElement>) => { const keys = ['ArrowLeft','ArrowRight','Home','End']; if (!keys.includes(event.key)) return; event.preventDefault(); const current = tabs.findIndex(([key]) => key === activeTab); const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; setActiveTab(tabs[next][0] as never); (event.currentTarget.querySelectorAll('button')[next] as HTMLButtonElement)?.focus() }
  return <><span className="sr-only" role="status" aria-live="polite">任务状态：{statusLabels[task.status]}</span><div className="task-detail-toolbar"><button className="back-to-list" type="button" onClick={backToList}>← 返回任务列表</button><div className="task-actions">{!terminal.has(task.status) && <button type="button" onClick={onCancel}>取消任务</button>}{terminal.has(task.status) && <button type="button" onClick={onRetry}>重试</button>}{terminal.has(task.status) && <button type="button" onClick={onDelete}>删除</button>}</div></div><div className="task-heading"><div><p className="eyebrow">任务 {task.task_id}</p><h2 ref={headingRef} tabIndex={-1}>{statusLabels[task.status]}</h2><p>{task.data_id ? `业务标识：${task.data_id}` : `创建于 ${formatDate(task.created_at)}`}</p></div><div className="task-meta"><span className="duration"><small>{durationLabel}</small><strong>{duration}</strong></span><span className={`badge large ${task.status}`}>{statusLabels[task.status]}</span></div></div>
  <span className="sr-only" role="status" aria-live="polite">{events.length ? `最新事件：${events.at(-1)?.message || events.at(-1)?.type}` : ''}</span><div className="timeline" tabIndex={0} role="region" aria-label="任务执行时间线">{events.length ? events.map((event) => <Timeline key={event.sequence} label={`${formatDate(event.at)} · ${event.message || event.type}`} state={event.sequence === events.length && !terminal.has(task.status) ? 'current' : 'done'} />) : <Timeline label="正在读取任务时间线" state="current" />}</div>
  {plans.length > 0 && <div className="plan-card"><p className="card-label">自动计划 · RULE-BASED</p>{plans.map((step, index) => <div className="plan-step" key={String(step.step_id)}><b>{index + 1}</b><div><strong>{String(step.skill_name)}</strong><p>{String(step.reason)}</p></div><span className={`badge ${String(step.status)}`}>{String(step.status)}</span></div>)}</div>}
  {(['failed','partial','cancelled','interrupted'] as TaskStatus[]).includes(task.status) && <div className="failure" role="status"><strong>{task.status === 'failed' ? '任务未完成' : task.status === 'partial' ? '任务部分完成' : task.status === 'cancelled' ? '任务已取消' : '任务被服务重启中断'}</strong><p>{String(task.error?.message || (task.result?.error as Json | undefined)?.message || (task.status === 'partial' ? '部分内容可用，请查看 warning 和产物。' : '可以重试任务或查看执行详情。'))}</p></div>}
  <nav className="tabs" role="tablist" aria-label="解析结果视图" onKeyDown={onTabKey}>{tabs.map(([key, label]) => <button key={key} id={`tab-${key}`} role="tab" tabIndex={activeTab === key ? 0 : -1} aria-selected={activeTab === key} aria-controls="result-panel" className={activeTab === key ? 'active' : ''} onClick={() => setActiveTab(key as never)}>{label}</button>)}</nav>
  <div id="result-panel" role="tabpanel" tabIndex={0} aria-labelledby={`tab-${activeTab}`}><ResultTab tab={activeTab} task={task} document={document} fileId={fileId} tables={tables} representations={representations} artifacts={artifacts} /></div>
  </>
}
function Timeline({ label, state }: { label: string; state: string }) { return <div className={`timeline-item ${state}`}><i>{state === 'done' ? '✓' : ''}</i><span>{label}</span></div> }
function CopyButton({ text }: { text: string }) {
 const [copied, setCopied] = useState(false)
 const copy = async () => {
  try {
   await navigator.clipboard.writeText(text)
   setCopied(true)
   window.setTimeout(() => setCopied(false), 1800)
  } catch { window.prompt('请复制以下解析结果：', text) }
 }
 return <div className="result-actions"><button type="button" className="copy-result" onClick={copy}>{copied ? '已复制 ✓' : '复制结果'}</button><span className="sr-only" role="status" aria-live="polite">{copied ? '解析结果已复制到剪贴板' : ''}</span></div>
}
function ArtifactItem({ artifact, taskId, index }: { artifact: Artifact; taskId: string; index: number }) {
 const filename = artifact.filename || `产物 ${index + 1}`
 const downloadUrl = artifact.download_url || `/api/v1/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`
 const image = artifact.content_type?.startsWith('image/')
 return <div className={`artifact ${image ? 'image-artifact' : ''}`}>{image ? <img src={downloadUrl} loading="lazy" alt={`${filename} 预览`} /> : <span aria-hidden="true">⌁</span>}<div><strong>{filename}</strong><small>{artifact.kind === 'export' ? '结果导出' : artifact.kind || '文件产物'}{artifact.size_bytes ? ` · ${fileSize(artifact.size_bytes)}` : ''}</small></div><a href={downloadUrl} download={filename}>下载</a></div>
}
function JsonView({ text, label }: { text: string; label: string }) { const [query, setQuery] = useState(''); const shown = query ? text.split('\n').filter((line) => line.toLowerCase().includes(query.toLowerCase())).join('\n') || '没有匹配内容' : text; return <div className="json-view"><div className="result-actions"><label>{label}搜索<input value={query} onChange={(event) => setQuery(event.target.value)} /></label><CopyButton text={text} /></div><details open><summary>{label}</summary><pre className="json">{shown}</pre></details></div> }
function ResultTab({ tab, task, document, fileId: _fileId, tables, representations, artifacts }: { tab: string; task: Task; document?: Json; fileId?: string; tables: Json[]; representations: Json; artifacts: Artifact[] }) {
 const plainText = String(representations.plain_text || '当前结果没有可用的纯文本表示。')
 const markdown = String(representations.markdown || '当前结果没有可用的 Markdown 表示。')
 const details = JSON.stringify({ plan: task.plan, error: task.error, warnings: task.warnings, result: task.result }, null, 2)
 const structured = JSON.stringify(document || task, null, 2)
 if (tab === 'overview') { const quality = task.result?.quality || {}; const provenance = task.result?.provenance || {}; const providers = Array.isArray(provenance.provider_chain) ? provenance.provider_chain.join(' → ') : '未知'; return <div className="overview"><Stat label="文档类型" value={String(document?.document_type || '等待结果')} /><Stat label="输入分类" value={String(quality.input_classification || '未知')} /><Stat label="表格" value={String(quality.tables ?? tables.length)} /><Stat label="图片" value={String(quality.images ?? ((document?.images as Json[] | undefined)?.length || 0))} /><Stat label="Provider 链路" value={providers} /><Stat label="截断" value={quality.truncated ? '是' : '否'} /><section><h3>解析摘要</h3><p>{plainText.slice(0, 600)}</p></section></div> }
 if (tab === 'text') return <><CopyButton text={plainText} /><pre className="text-preview">{plainText}</pre></>
 if (tab === 'markdown') return <><CopyButton text={markdown} /><pre className="text-preview markdown">{markdown}</pre></>
 if (tab === 'html') return representations.html ? <iframe className="html-preview" title="HTML 内容预览" sandbox="" srcDoc={String(representations.html)} /> : <NoContent text="当前结果没有可用的 HTML 表示。" />
 if (tab === 'tables') return <div className="tables">{tables.length ? tables.map((table, index) => <Table key={index} table={table} />) : <NoContent text="当前结果没有可预览的表格。" />}</div>
 if (tab === 'artifacts') return <div className="artifacts">{artifacts.length ? artifacts.map((artifact, i) => <ArtifactItem artifact={artifact} taskId={task.task_id} index={i} key={artifact.artifact_id} />) : <NoContent text="当前结果没有可下载的 artifact。" />}</div>
 if (tab === 'details') return <JsonView text={details} label="执行详情" />
 return <JsonView text={structured} label="结构化 JSON" />
}
function Stat({ label, value }: { label: string; value: string }) { return <div className="stat"><span>{label}</span><strong>{value}</strong></div> }
function NoContent({ text }: { text: string }) { return <div className="no-content">{text}</div> }
function tableRow(row: unknown): unknown[] {
  if (Array.isArray(row)) return row
  return typeof row === 'string' ? row.split(' | ') : [row]
}
export function Table({ table }: { table: Json }) {
  const [page, setPage] = useState(0); const pageSize = 50
  useEffect(() => setPage(0), [table])
  const sourceRows = Array.isArray(table.rows) ? table.rows : []; const rows = sourceRows.map(tableRow)
  const title = String(table.sheet_name || table.name || '解析表格'); const range = table.range ? String(table.range) : ''
  const header = rows[0]; const pages = Math.max(1, Math.ceil(Math.max(0, rows.length - 1) / pageSize)); const currentPage = Math.min(page, pages - 1); const body = rows.slice(1 + currentPage * pageSize, 1 + (currentPage + 1) * pageSize)
  return <section className="table-wrap"><p className="table-title"><strong>{title}</strong><span>{range || `${rows.length} 行`}</span></p><div className="table-scroll"><table><caption className="sr-only">{title}，第 {currentPage + 1} 页，共 {pages} 页</caption>{header && <thead><tr><th className="table-row-number" scope="col">#</th>{header.map((cell, j) => <th key={j} scope="col">{String(cell ?? '')}</th>)}</tr></thead>}<tbody>{body.map((row, i) => <tr key={i}><th className="table-row-number" scope="row">{currentPage * pageSize + i + 1}</th>{row.map((cell, j) => <td key={j}>{String(cell ?? '')}</td>)}</tr>)}</tbody></table></div>{pages > 1 && <nav className="table-pagination" aria-label={`${title} 分页`}><button type="button" disabled={currentPage === 0} onClick={() => setPage((value) => value - 1)}>上一页</button><span>{currentPage + 1} / {pages}</span><button type="button" disabled={currentPage + 1 >= pages} onClick={() => setPage((value) => value + 1)}>下一页</button></nav>}</section>
}

const root = document.getElementById('root')
if (root) createRoot(root).render(<App />)
