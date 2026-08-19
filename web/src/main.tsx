import { ChangeEvent, DragEvent, FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import './dashboard.css'

type Json = Record<string, unknown>
type TaskStatus = 'queued' | 'planning' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled'
type Task = { task_id: string; status: TaskStatus; data_id?: string; created_at: string; started_at?: string; finished_at?: string; duration_ms?: number; error?: Json; plan?: Json; result?: Json; callback_status?: string }
const terminal = new Set<TaskStatus>(['succeeded', 'partial', 'failed', 'cancelled'])
const recentKey = 'parse-agent-recent-tasks'

const statusLabels: Record<TaskStatus, string> = { queued: '等待处理', planning: '正在制定解析方案', running: '正在执行解析', succeeded: '解析完成', partial: '部分完成', failed: '解析失败', cancelled: '已取消' }

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error((body.detail?.message || body.detail || body.message || `请求失败：${response.status}`) as string)
  return body as T
}
function documentOf(task?: Task | null): Json | undefined {
  const result = task?.result?.result as Json | undefined
  const data = result?.data as Json | undefined
  return data?.document as Json | undefined
}
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
  const [activeTab, setActiveTab] = useState<'overview' | 'text' | 'tables' | 'markdown' | 'json' | 'artifacts' | 'details'>('overview')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!task || terminal.has(task.status)) return
    const id = window.setInterval(async () => {
      try { const next = await api<Task>(`/api/v1/tasks/${task.task_id}`); setTask(next); storeRecent(next); setRecent(JSON.parse(localStorage.getItem(recentKey) || '[]')) } catch (err) { setError(err instanceof Error ? err.message : '任务状态查询失败') }
    }, 1000)
    return () => window.clearInterval(id)
  }, [task?.task_id, task?.status])

  const document = useMemo(() => documentOf(task), [task])
  const choose = (candidate?: File) => { if (candidate) { setFile(candidate); setError('') } }
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!file) return setError('请先选择一个文件')
    setSubmitting(true); setError('')
    try {
      const form = new FormData(); form.append('file', file); if (goal.trim()) form.append('goal', goal.trim())
      const created = await api<Task>('/api/v1/parse', { method: 'POST', body: form })
      setTask(created); storeRecent(created); setRecent(JSON.parse(localStorage.getItem(recentKey) || '[]')); setActiveTab('overview')
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
  const artifacts = ((document?.images as Json[] | undefined) || []).filter((item) => item.artifact_path || item.path)

  return <main className="app-shell">
    <header className="app-header"><span className="brand-mark" aria-hidden="true">✦</span><div><p className="eyebrow">PARSEFLOW · 0.6.0</p><h1>智能文档工作台</h1><p className="subtitle">上传一个文件，系统会自动规划并执行合适的解析流程。</p></div><span className="service"><i /> 服务就绪</span></header>
    <section className="workspace-grid">
      <aside className="upload-panel"><h2>开始解析</h2><form onSubmit={submit}>
        <div className="dropzone" role="button" tabIndex={0} aria-label="选择要解析的文件" onDrop={onDrop} onDragOver={(event) => event.preventDefault()} onClick={openFilePicker} onKeyDown={onDropzoneKeyDown}>
          <input ref={inputRef} type="file" onChange={(event: ChangeEvent<HTMLInputElement>) => choose(event.target.files?.[0])} hidden />
          <span className="upload-icon">↑</span><strong>{file ? file.name : '拖拽文件到这里'}</strong><small>{file ? `${file.type || '未知类型'} · ${fileSize(file.size)}` : '或点击选择 PDF、Office、RTF、XML、CSV、音视频等文件'}</small>
        </div>
        <SupportedFormats />
        <label>解析目标 <span>可选</span><textarea value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：提取表格并输出 Markdown" rows={3} /></label>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={submitting}>{submitting ? '正在创建任务…' : '开始智能解析'} <span>→</span></button>
      </form>
      <div className="hint"><strong>自动规划</strong><p>系统根据文件类型选择已注册的解析能力；复杂 RTF、旧版 Office 等会自动走增强处理路径。</p></div></aside>
      <section className="content-panel">{task ? <TaskView task={task} document={document} plans={plans} tables={tables} representations={representations} artifacts={artifacts} activeTab={activeTab} setActiveTab={setActiveTab} backToList={() => { setTask(null); setActiveTab('overview'); setError('') }} /> : <EmptyState recent={recent} open={(item) => { setTask(item); setActiveTab('overview'); setError('') }} />}</section>
    </section>
  </main>
}

function SupportedFormats() {
  const formats = [
    ['文档', 'PDF · DOC · DOCX · RTF'],
    ['表格', 'XLS · XLSX · XLSM · CSV · TSV'],
    ['演示', 'PPT · PPTX'],
    ['文本与数据', 'TXT · MD · HTML · XML'],
    ['音频', 'MP3 · WAV · M4A · AAC · FLAC · OGG'],
    ['视频', 'MP4 · MOV · MKV · AVI · WEBM'],
  ]
  return <section className="formats-guide" aria-labelledby="formats-title"><div className="formats-heading"><div><p className="formats-kicker">SUPPORTED FORMATS</p><h3 id="formats-title">支持的文件格式</h3></div><span>24+ 种</span></div><div className="format-list">{formats.map(([category, extensions]) => <div className="format-item" key={category}><strong>{category}</strong><span>{extensions}</span></div>)}</div><p className="formats-note">系统会根据文件类型自动选择合适的解析或预处理流程。</p></section>
}
function EmptyState({ recent, open }: { recent: Task[]; open: (task: Task) => void }) { return <div className="empty"><div className="empty-mark">✦</div><h2>等待文件</h2><p>上传后，系统会创建任务、展示自动解析方案，并在这里呈现可用结果。</p>{recent.length > 0 && <div className="recent"><h3>最近任务</h3>{recent.map((item) => <button key={item.task_id} onClick={() => open(item)}><span>{item.task_id.slice(0, 16)}…</span><em className={`badge ${item.status}`}>{statusLabels[item.status]}</em></button>)}</div>}</div> }

function TaskView({ task, document, plans, tables, representations, artifacts, activeTab, setActiveTab, backToList }: { task: Task; document?: Json; plans: Json[]; tables: Json[]; representations: Json; artifacts: Json[]; activeTab: string; setActiveTab: (tab: never) => void; backToList: () => void }) {
  const tabs = [['overview','概览'],['text','正文'],['tables',`表格 ${tables.length || ''}`],['markdown','Markdown'],['json','结构化 JSON'],['artifacts',`产物 ${artifacts.length || ''}`],['details','执行详情']] as const
  const duration = taskDuration(task)
  const durationLabel = terminal.has(task.status) ? '整体执行时间' : '已用时间'
  return <><div className="task-detail-toolbar"><button className="back-to-list" type="button" onClick={backToList}>← 返回任务列表</button></div><div className="task-heading"><div><p className="eyebrow">任务 {task.task_id}</p><h2>{statusLabels[task.status]}</h2><p>{task.data_id ? `业务标识：${task.data_id}` : `创建于 ${formatDate(task.created_at)}`}</p></div><div className="task-meta"><span className="duration"><small>{durationLabel}</small><strong>{duration}</strong></span><span className={`badge large ${task.status}`}>{statusLabels[task.status]}</span></div></div>
  <div className="timeline"><Timeline label="任务已创建" state="done" /><Timeline label="自动规划" state={task.plan ? 'done' : task.status === 'queued' ? 'current' : 'waiting'} /><Timeline label="执行解析" state={task.status === 'running' ? 'current' : terminal.has(task.status) ? 'done' : 'waiting'} /><Timeline label="生成结果" state={terminal.has(task.status) ? 'done' : 'waiting'} /></div>
  {plans.length > 0 && <div className="plan-card"><p className="card-label">自动计划 · RULE-BASED</p>{plans.map((step, index) => <div className="plan-step" key={String(step.step_id)}><b>{index + 1}</b><div><strong>{String(step.skill_name)}</strong><p>{String(step.reason)}</p></div><span className={`badge ${String(step.status)}`}>{String(step.status)}</span></div>)}</div>}
  {task.status === 'failed' && <div className="failure"><strong>任务未完成</strong><p>{String(task.error?.message || (task.result?.error as Json | undefined)?.message || '请检查执行详情。')}</p></div>}
  <nav className="tabs" role="tablist" aria-label="解析结果视图">{tabs.map(([key, label]) => <button key={key} id={`tab-${key}`} role="tab" aria-selected={activeTab === key} aria-controls="result-panel" className={activeTab === key ? 'active' : ''} onClick={() => setActiveTab(key as never)}>{label}</button>)}</nav>
  <div id="result-panel" role="tabpanel" aria-labelledby={`tab-${activeTab}`}><ResultTab tab={activeTab} task={task} document={document} tables={tables} representations={representations} artifacts={artifacts} /></div>
  </>
}
function Timeline({ label, state }: { label: string; state: string }) { return <div className={`timeline-item ${state}`}><i>{state === 'done' ? '✓' : ''}</i><span>{label}</span></div> }
function ResultTab({ tab, task, document, tables, representations, artifacts }: { tab: string; task: Task; document?: Json; tables: Json[]; representations: Json; artifacts: Json[] }) {
 if (tab === 'overview') return <div className="overview"><Stat label="文档类型" value={String(document?.document_type || '等待结果')} /><Stat label="表格" value={String(tables.length)} /><Stat label="图片 / 产物" value={String((document?.images as Json[] | undefined)?.length || 0)} /><Stat label="任务状态" value={statusLabels[task.status]} /><section><h3>解析摘要</h3><p>{String(representations.plain_text || '任务完成后，这里将展示正文摘要。').slice(0, 600)}</p></section></div>
 if (tab === 'text') return <pre className="text-preview">{String(representations.plain_text || '当前结果没有可用的纯文本表示。')}</pre>
 if (tab === 'markdown') return <pre className="text-preview markdown">{String(representations.markdown || '当前结果没有可用的 Markdown 表示。')}</pre>
 if (tab === 'tables') return <div className="tables">{tables.length ? tables.map((table, index) => <Table key={index} table={table} />) : <NoContent text="当前结果没有可预览的表格。" />}</div>
 if (tab === 'artifacts') return <div className="artifacts">{artifacts.length ? artifacts.map((artifact, i) => <div className="artifact" key={i}><span>⌁</span><div><strong>{String(artifact.filename || artifact.path || `产物 ${i + 1}`)}</strong><small>{String(artifact.kind || '文件产物')}</small></div></div>) : <NoContent text="当前结果没有可下载的 artifact。" />}</div>
 if (tab === 'details') return <pre className="json">{JSON.stringify({ plan: task.plan, callback_status: task.callback_status, error: task.error, result: task.result }, null, 2)}</pre>
 return <pre className="json">{JSON.stringify(document || task, null, 2)}</pre>
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
