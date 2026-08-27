import { ChangeEvent, DragEvent, FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import './dashboard.css'

type Json = Record<string, unknown>
type TaskStatus = 'queued' | 'planning' | 'running' | 'cancelling' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
type Artifact = { artifact_id: string; filename: string; content_type?: string; size_bytes?: number; kind?: string; download_url?: string }
type TaskResult = { status: 'succeeded' | 'partial' | 'failed'; document?: Json; artifacts: Artifact[]; steps: Json[]; conversion?: Json; warnings: string[]; metrics: Json; error?: Json }
type Task = { task_id: string; file_id: string; status: TaskStatus; data_id?: string; goal?: string; retry_of?: string; created_at: string; updated_at?: string; started_at?: string; finished_at?: string; duration_ms?: number; error?: Json; plan?: Json; result?: TaskResult; warnings?: string[] }
type TaskEvent = { sequence: number; type: string; at: string; step?: string; message?: string; details: Json }
type SubmittedTask = { task_id: string; file_id: string; status: 'queued'; created_at: string }
const terminal = new Set<TaskStatus>(['succeeded', 'partial', 'failed', 'cancelled', 'interrupted'])
const recentKey = 'parse-agent-recent-tasks'

const statusLabels: Record<TaskStatus, string> = { queued: '等待处理', planning: '正在制定解析方案', running: '正在执行解析', cancelling: '正在取消', succeeded: '解析完成', partial: '部分完成', failed: '解析失败', cancelled: '已取消', interrupted: '服务中断' }

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error((body.detail?.message || body.detail || body.message || `请求失败：${response.status}`) as string)
  return body as T
}
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
function storeRecent(task: Task) {
  const old = JSON.parse(localStorage.getItem(recentKey) || '[]') as Task[]
  const items = [task, ...old.filter((item) => item.task_id !== task.task_id)].slice(0, 8)
  localStorage.setItem(recentKey, JSON.stringify(items))
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

function App() {
  const [file, setFile] = useState<File | null>(null)
  const [goal, setGoal] = useState('')
  const [task, setTask] = useState<Task | null>(null)
  const [recent, setRecent] = useState<Task[]>(() => JSON.parse(localStorage.getItem(recentKey) || '[]'))
  const [activeTab, setActiveTab] = useState<'overview' | 'text' | 'tables' | 'markdown' | 'html' | 'json' | 'artifacts' | 'details'>('overview')
  const [error, setError] = useState('')
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    api<{ items: Task[] }>('/api/v1/tasks?limit=20&sort=created_desc').then((payload) => setRecent(payload.items)).catch(() => undefined)
    const match = window.location.pathname.match(/^\/tasks\/(task_[0-9a-f]{32})$/)
    if (match) api<Task>(`/api/v1/tasks/${match[1]}`).then(setTask).catch((err) => setError(err instanceof Error ? err.message : '无法恢复任务'))
  }, [])

  useEffect(() => {
    if (!task || terminal.has(task.status)) return
    const id = window.setInterval(async () => {
      try { const next = await api<Task>(`/api/v1/tasks/${task.task_id}`); setTask(next); storeRecent(next); setRecent(JSON.parse(localStorage.getItem(recentKey) || '[]')) } catch (err) { setError(err instanceof Error ? err.message : '任务状态查询失败') }
    }, 1000)
    return () => window.clearInterval(id)
  }, [task?.task_id, task?.status])

  useEffect(() => {
    if (!task) { setEvents([]); return }
    let stopped = false
    const refresh = () => api<{ items: TaskEvent[] }>(`/api/v1/tasks/${task.task_id}/events?limit=500`)
      .then((payload) => { if (!stopped) setEvents(payload.items) }).catch(() => undefined)
    refresh()
    if (terminal.has(task.status)) return () => { stopped = true }
    const id = window.setInterval(refresh, globalThis.document.hidden ? 5000 : 1500)
    return () => { stopped = true; window.clearInterval(id) }
  }, [task?.task_id, task?.status])

  const openTask = (item: Task) => { setTask(item); setActiveTab('overview'); setError(''); window.history.pushState({}, '', `/tasks/${item.task_id}`) }
  const retryTask = async () => { if (!task) return; const created = await api<SubmittedTask>(`/api/v1/tasks/${task.task_id}/retry`, { method: 'POST' }); openTask(await api<Task>(`/api/v1/tasks/${created.task_id}`)) }
  const cancelTask = async () => { if (task) setTask(await api<Task>(`/api/v1/tasks/${task.task_id}/cancel`, { method: 'POST' })) }
  const deleteTask = async () => { if (!task) return; await fetch(`/api/v1/tasks/${task.task_id}`, { method: 'DELETE' }); setTask(null); setEvents([]); window.history.pushState({}, '', '/'); setRecent((items) => items.filter((item) => item.task_id !== task.task_id)) }

  const document = useMemo(() => documentOf(task), [task])
  const choose = (candidate?: File) => { if (candidate) { setFile(candidate); setError('') } }
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!file) return setError('请先选择一个文件')
    setSubmitting(true); setError('')
    try {
      const form = new FormData(); form.append('file', file); if (goal.trim()) form.append('goal', goal.trim())
      const created = await api<SubmittedTask>('/api/v1/tasks/parse', { method: 'POST', body: form })
      const createdTask = await api<Task>(`/api/v1/tasks/${created.task_id}`)
      openTask(createdTask); storeRecent(createdTask); setRecent((items) => [createdTask, ...items.filter((item) => item.task_id !== createdTask.task_id)])
    } catch (err) { setError(err instanceof Error ? err.message : '提交失败') } finally { setSubmitting(false) }
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

  return <main className="app-shell">
    <header className="app-header"><span className="brand-mark" aria-hidden="true">✦</span><div><p className="eyebrow">PARSEFLOW · 1.1.0</p><h1>智能文档工作台</h1><p className="subtitle">上传一个文件，系统会自动规划并执行合适的解析流程。</p></div><span className="service"><i /> 服务就绪</span></header>
    <section className="workspace-grid">
      <aside className="upload-panel"><h2>开始解析</h2><form onSubmit={submit}>
        <div className="dropzone" role="button" tabIndex={0} aria-label="选择要解析的文件" onDrop={onDrop} onDragOver={(event) => event.preventDefault()} onClick={openFilePicker} onKeyDown={onDropzoneKeyDown}>
          <input ref={inputRef} type="file" onChange={(event: ChangeEvent<HTMLInputElement>) => choose(event.target.files?.[0])} hidden />
          <span className="upload-icon">↑</span><strong>{file ? file.name : '拖拽文件到这里'}</strong><small>{file ? `${file.type || '未知类型'} · ${fileSize(file.size)}` : '或点击选择 PDF、图片、Office、RTF、XML、CSV、音视频等文件'}</small>
        </div>
        <SupportedFormats />
        <label>解析目标 <span>可选</span><textarea value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：提取表格并输出 Markdown" rows={3} /></label>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={submitting}>{submitting ? '正在创建任务…' : '开始智能解析'} <span>→</span></button>
      </form>
      <div className="hint"><strong>自动规划</strong><p>系统根据文件类型选择已注册的解析能力；复杂 RTF、旧版 Office 等会自动走增强处理路径。</p></div></aside>
      <section className="content-panel">{task ? <TaskView task={task} events={events} document={document} fileId={fileId} plans={plans} tables={tables} representations={representations} artifacts={artifacts} activeTab={activeTab} setActiveTab={setActiveTab} onCancel={cancelTask} onRetry={retryTask} onDelete={deleteTask} backToList={() => { setTask(null); setActiveTab('overview'); setError(''); window.history.pushState({}, '', '/') }} /> : <EmptyState recent={recent} open={openTask} />}</section>
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
function EmptyState({ recent, open }: { recent: Task[]; open: (task: Task) => void }) { return <div className="empty"><div className="empty-mark">✦</div><h2>等待文件</h2><p>上传后，系统会创建任务、展示自动解析方案，并在这里呈现可用结果。</p>{recent.length > 0 && <div className="recent"><h3>最近任务</h3>{recent.map((item) => <button key={item.task_id} onClick={() => open(item)}><span>{item.task_id.slice(0, 16)}…</span><em className={`badge ${item.status}`}>{statusLabels[item.status]}</em></button>)}</div>}</div> }

function TaskView({ task, events, document, fileId, plans, tables, representations, artifacts, activeTab, setActiveTab, onCancel, onRetry, onDelete, backToList }: { task: Task; events: TaskEvent[]; document?: Json; fileId?: string; plans: Json[]; tables: Json[]; representations: Json; artifacts: Artifact[]; activeTab: string; setActiveTab: (tab: never) => void; onCancel: () => void; onRetry: () => void; onDelete: () => void; backToList: () => void }) {
  const tabs = [['overview','概览'],['text','正文'],['tables',`表格 ${tables.length || ''}`],['markdown','Markdown'],['html','HTML 预览'],['json','结构化 JSON'],['artifacts',`产物 ${artifacts.length || ''}`],['details','执行详情']] as const
  const duration = taskDuration(task)
  const durationLabel = terminal.has(task.status) ? '整体执行时间' : '已用时间'
  return <><div className="task-detail-toolbar"><button className="back-to-list" type="button" onClick={backToList}>← 返回任务列表</button><div className="task-actions">{!terminal.has(task.status) && <button type="button" onClick={onCancel}>取消任务</button>}{terminal.has(task.status) && <button type="button" onClick={onRetry}>重试</button>}{terminal.has(task.status) && <button type="button" onClick={onDelete}>删除</button>}</div></div><div className="task-heading"><div><p className="eyebrow">任务 {task.task_id}</p><h2>{statusLabels[task.status]}</h2><p>{task.data_id ? `业务标识：${task.data_id}` : `创建于 ${formatDate(task.created_at)}`}</p></div><div className="task-meta"><span className="duration"><small>{durationLabel}</small><strong>{duration}</strong></span><span className={`badge large ${task.status}`}>{statusLabels[task.status]}</span></div></div>
  <div className="timeline" aria-live="polite">{events.length ? events.map((event) => <Timeline key={event.sequence} label={`${formatDate(event.at)} · ${event.message || event.type}`} state={event.sequence === events.length && !terminal.has(task.status) ? 'current' : 'done'} />) : <Timeline label="正在读取任务时间线" state="current" />}</div>
  {plans.length > 0 && <div className="plan-card"><p className="card-label">自动计划 · RULE-BASED</p>{plans.map((step, index) => <div className="plan-step" key={String(step.step_id)}><b>{index + 1}</b><div><strong>{String(step.skill_name)}</strong><p>{String(step.reason)}</p></div><span className={`badge ${String(step.status)}`}>{String(step.status)}</span></div>)}</div>}
  {task.status === 'failed' && <div className="failure"><strong>任务未完成</strong><p>{String(task.error?.message || (task.result?.error as Json | undefined)?.message || '请检查执行详情。')}</p></div>}
  <nav className="tabs" role="tablist" aria-label="解析结果视图">{tabs.map(([key, label]) => <button key={key} id={`tab-${key}`} role="tab" aria-selected={activeTab === key} aria-controls="result-panel" className={activeTab === key ? 'active' : ''} onClick={() => setActiveTab(key as never)}>{label}</button>)}</nav>
  <div id="result-panel" role="tabpanel" aria-labelledby={`tab-${activeTab}`}><ResultTab tab={activeTab} task={task} document={document} fileId={fileId} tables={tables} representations={representations} artifacts={artifacts} /></div>
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
 return <div className="artifact"><span aria-hidden="true">⌁</span><div><strong>{filename}</strong><small>{artifact.kind || '文件产物'}</small></div><a href={downloadUrl} download={filename}>下载</a></div>
}
function ResultTab({ tab, task, document, fileId: _fileId, tables, representations, artifacts }: { tab: string; task: Task; document?: Json; fileId?: string; tables: Json[]; representations: Json; artifacts: Artifact[] }) {
 const plainText = String(representations.plain_text || '当前结果没有可用的纯文本表示。')
 const markdown = String(representations.markdown || '当前结果没有可用的 Markdown 表示。')
 const details = JSON.stringify({ plan: task.plan, error: task.error, warnings: task.warnings, result: task.result }, null, 2)
 const structured = JSON.stringify(document || task, null, 2)
 if (tab === 'overview') return <div className="overview"><Stat label="文档类型" value={String(document?.document_type || '等待结果')} /><Stat label="表格" value={String(tables.length)} /><Stat label="图片 / 产物" value={String((document?.images as Json[] | undefined)?.length || 0)} /><Stat label="任务状态" value={statusLabels[task.status]} /><section><h3>解析摘要</h3><p>{plainText.slice(0, 600)}</p></section></div>
 if (tab === 'text') return <><CopyButton text={plainText} /><pre className="text-preview">{plainText}</pre></>
 if (tab === 'markdown') return <><CopyButton text={markdown} /><pre className="text-preview markdown">{markdown}</pre></>
 if (tab === 'html') return representations.html ? <iframe className="html-preview" title="HTML 内容预览" sandbox="" srcDoc={String(representations.html)} /> : <NoContent text="当前结果没有可用的 HTML 表示。" />
 if (tab === 'tables') return <div className="tables">{tables.length ? tables.map((table, index) => <Table key={index} table={table} />) : <NoContent text="当前结果没有可预览的表格。" />}</div>
 if (tab === 'artifacts') return <div className="artifacts">{artifacts.length ? artifacts.map((artifact, i) => <ArtifactItem artifact={artifact} taskId={task.task_id} index={i} key={artifact.artifact_id} />) : <NoContent text="当前结果没有可下载的 artifact。" />}</div>
 if (tab === 'details') return <><CopyButton text={details} /><pre className="json">{details}</pre></>
 return <><CopyButton text={structured} /><pre className="json">{structured}</pre></>
}
function Stat({ label, value }: { label: string; value: string }) { return <div className="stat"><span>{label}</span><strong>{value}</strong></div> }
function NoContent({ text }: { text: string }) { return <div className="no-content">{text}</div> }
function tableRow(row: unknown): unknown[] {
  if (Array.isArray(row)) return row
  return typeof row === 'string' ? row.split(' | ') : [row]
}
function Table({ table }: { table: Json }) {
  const sourceRows = Array.isArray(table.rows) ? table.rows : []
  const rows = sourceRows.map(tableRow)
  const title = String(table.sheet_name || table.name || '解析表格')
  const range = table.range ? String(table.range) : ''
  const [header, ...body] = rows.slice(0, 50)
  return <section className="table-wrap"><p className="table-title"><strong>{title}</strong><span>{range || `${rows.length} 行`}</span></p><div className="table-scroll"><table aria-label={title}>{header && <thead><tr><th className="table-row-number" scope="col">#</th>{header.map((cell, j) => <th key={j} scope="col">{String(cell ?? '')}</th>)}</tr></thead>}<tbody>{body.map((row, i) => <tr key={i}><td className="table-row-number">{i + 1}</td>{row.map((cell, j) => <td key={j}>{String(cell ?? '')}</td>)}</tr>)}</tbody></table></div>{rows.length > 50 && <small className="table-note">为保证浏览流畅度，当前仅预览前 50 行。</small>}</section>
}

createRoot(document.getElementById('root')!).render(<App />)
