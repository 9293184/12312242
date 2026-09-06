import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // envDir 用相对路径 '.',避免依赖 @types/node 的 process 类型
  const env = loadEnv(mode, '.', '')
  return {
  // 根路径部署必须使用绝对 base('/')。
  // 原因:BrowserRouter 深链接(如 /papers/:id)直接打开/刷新时,浏览器按"当前 URL 所在目录"
  // 解析相对资源——base './' 会让 ./assets/index-*.js 被解析成 /papers/assets/index-*.js 而 404,
  // 同理 ./icon.png、./poem.md 在深链接下也会 404。绝对 base 下资源始终从站点根解析。
  // 如需子路径部署(如 http://host/paperpilot/),在 .env 中设置 VITE_BASE=/paperpilot/(首尾斜杠)。
  base: env.VITE_BASE || '/',
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 生产构建关闭 sourcemap,减小体积并避免暴露源码
    sourcemap: false,
    // 输出到 dist 目录
    outDir: 'dist',
    // 清空输出目录
    emptyOutDir: true,
    // 大依赖单独分包,优化首屏加载
    rollupOptions: {
      output: {
        manualChunks: {
          // React 核心
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          // PDF 阅读器相关(pdfjs-dist 较大,单独打包)
          'pdf-vendor': ['pdfjs-dist', 'react-pdf-highlighter-plus'],
          // Markdown 渲染 + 数学公式
          'markdown-vendor': ['react-markdown', 'remark-gfm', 'remark-math', 'rehype-katex', 'katex'],
        },
      },
    },
    // 提高大依赖阈值,避免过多小 chunk
    chunkSizeWarningLimit: 1500,
  },
  }
})
