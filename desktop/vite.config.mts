import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  root: 'src/renderer',
  base: './',
  plugins: [react()],
  publicDir: '../../build/public',
  build: { outDir: '../../dist/renderer', emptyOutDir: true, target: 'chrome140' },
  server: { port: 5199, strictPort: true },
});
