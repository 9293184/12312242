/**
 * poem.md 表格解析。
 *
 * App 的首页与个性化欢迎页此前各写了一份完全相同的解析逻辑，这里统一。
 */
export type ParsedPoem = {
  verse: string
  source: string
  author: string
}

/** 解析 poem.md 中的表格行，返回 {verse, source, author} 列表。 */
export function parsePoemTable(markdown: string): ParsedPoem[] {
  const lines = markdown.split('\n').filter((l) => l.trim())
  const poems: ParsedPoem[] = []
  for (const line of lines) {
    if (!line.startsWith('|') || line.includes(':---') || line.includes('诗句')) continue
    const parts = line.split('|').map((p) => p.trim()).filter(Boolean)
    if (parts.length >= 3) {
      poems.push({
        verse: parts[0].replace(/\s+/g, ' ').trim(),
        source: parts[1] || '',
        author: parts[2] || '',
      })
    }
  }
  return poems
}
