/**
 * 论文「处理中」状态集合。
 *
 * 侧边栏、论文详情页、标签管理页与全局轮询都需要判断一篇论文是否仍在
 * 处理中；此前该数组在 5 处各写了一份，容易漂移。这里统一维护。
 */
export const ANALYZING_STATUSES = [
  'uploaded',
  'mineru_processing',
  'mineru_converted',
  'ocr_fallback',
  'text_extracting',
  'metadata_extracting',
  'analyzing',
  'parsed',
  'duplicate_detected',
] as const

const ANALYZING_SET: ReadonlySet<string> = new Set(ANALYZING_STATUSES)

/** 该状态是否属于「处理中」（尚未进入终态）。 */
export function isAnalyzingStatus(status: string): boolean {
  return ANALYZING_SET.has(status)
}

/**
 * 论文状态机的终态。
 *
 * - `done` / `failed`：分析已完成或失败
 * - `imported`：仅题录、没有 PDF（如从 Zotero 导入），不存在可运行的分析
 */
export const TERMINAL_STATUSES = ['done', 'failed', 'imported'] as const

const TERMINAL_SET: ReadonlySet<string> = new Set(TERMINAL_STATUSES)

/** 该状态是否为终态（不会再变化，也没有正在跑的任务）。 */
export function isTerminalStatus(status: string): boolean {
  return TERMINAL_SET.has(status)
}
