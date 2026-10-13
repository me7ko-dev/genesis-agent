// npm run dev — the window from Vite's dev server (hot reload), the main
// process rebuilt once. Restart this after changing src/main.
import { spawn } from 'node:child_process';
import { createServer } from 'vite';

const run = (cmd, args, env = {}) =>
  spawn(cmd, args, { stdio: 'inherit', shell: true, env: { ...process.env, ...env } });

await new Promise((resolve, reject) => {
  run('npm', ['run', 'build:main']).on('exit', (code) => (code === 0 ? resolve() : reject(new Error('build:main failed'))));
});

const server = await createServer({ configFile: 'vite.config.mts' });
await server.listen();
const url = server.resolvedUrls.local[0];

const electron = run('npx', ['electron', '.'], { GENESIS_DESKTOP_DEV_URL: url });
electron.on('exit', async () => {
  await server.close();
  process.exit(0);
});
