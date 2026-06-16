import { defineConfig } from 'vite';
import { resolve } from 'path';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const BACKEND_PORT = 4201;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  server: {
    port: 4200,
    proxy: {
      '/ws': { target: `http://localhost:${BACKEND_PORT}`, ws: true },
      '/health': `http://localhost:${BACKEND_PORT}`,
      '/services': `http://localhost:${BACKEND_PORT}`,
      '/state': `http://localhost:${BACKEND_PORT}`,
      '/errors': `http://localhost:${BACKEND_PORT}`,
      '/canvas': `http://localhost:${BACKEND_PORT}`,
      '/validate': `http://localhost:${BACKEND_PORT}`,
      '/suggest-defaults': `http://localhost:${BACKEND_PORT}`,
      '/reset': `http://localhost:${BACKEND_PORT}`,
      '/events': `http://localhost:${BACKEND_PORT}`,
      '/deploy': `http://localhost:${BACKEND_PORT}`,
      '/destroy': `http://localhost:${BACKEND_PORT}`,
      '/destroy-all': `http://localhost:${BACKEND_PORT}`,
      '/vm': `http://localhost:${BACKEND_PORT}`,
      '/invoke': `http://localhost:${BACKEND_PORT}`,
    },
    fs: {
      allow: [resolve(__dirname, '..')],
    },
  },
});
