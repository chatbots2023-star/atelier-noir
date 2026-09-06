import { defineConfig, loadEnv } from 'vite'
import { pixMiddleware } from './server/pix.js'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  process.env.PUSHINPAY_TOKEN = process.env.PUSHINPAY_TOKEN || env.PUSHINPAY_TOKEN
  process.env.PIX_VALUE_CENTS = process.env.PIX_VALUE_CENTS || env.PIX_VALUE_CENTS || '1999'
  process.env.ACCESS_SECRET = process.env.ACCESS_SECRET || env.ACCESS_SECRET

  return {
    build: {
      rollupOptions: {
        input: {
          main: 'index.html',
          book: 'book.html'
        }
      }
    },
    server: {
      host: true,
      port: 5173,
      allowedHosts: ['.monkeycode-ai.live']
    },
    preview: {
      host: true,
      port: 4173,
      allowedHosts: ['.monkeycode-ai.live']
    },
    plugins: [
      {
        name: 'pushinpay-api',
        configureServer(server) {
          server.middlewares.use(pixMiddleware)
        },
        configurePreviewServer(server) {
          server.middlewares.use(pixMiddleware)
        }
      }
    ]
  }
})
