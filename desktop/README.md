# Genesis Desktop

The Genesis window for Windows (Electron + React + Vite), in the style of
Claude Code. It carries no agent of its own: it starts the `genesis` the
terminal runs (`genesis serve --bind 127.0.0.1`, first on PATH — pipx — then
the installer's folder) and talks to it over the same encrypted protocol as
the phone app (`genesis_agent/remote_server.py`). The window's own commands
(/usage, /status, /model, /history, /export…) are in
`genesis_agent/desktop_commands.py`.

## Build and run

```
npm ci
npm run dev        # window with hot reload
npm run build      # main + renderer into dist/
npx electron .     # the built window
npm test           # node --test, tests/*.test.ts
npm run typecheck
```

Installers are built in CI, not on a machine with Smart App Control (it blocks a
freshly built, unsigned installer): `.github/workflows/native.yml`, job
`desktop` → artifact `genesis-desktop-setup`. Every push to main puts
`Genesis-Setup-*.exe` in the release next to `genesis-windows-x64.zip`.

## Checking it live

The window has one instance per profile, so a check runs in its own profile
and never touches the settings or the open window of whoever uses it:

```
npx electron . --remote-debugging-port=9333 --user-data-dir=<temp folder>
node scripts/cdp.mjs eval "window.genesis.openWorkspace('C:/path/to/project').then(s => s.phase)"
node scripts/cdp.mjs type "/status"
node scripts/cdp.mjs shot status
```

Check an installed build the way its shortcut starts it — in its own folder
(`Start-Process Genesis.exe -WorkingDirectory <its folder>`): `where genesis`
looks in the current folder first, and that is where `Genesis.exe` is.

Environment for checks: `GENESIS_DESKTOP_DEBUG=1` (state and events on
stdout), `GENESIS_DESKTOP_SHOT=out.png,<ms>[,scrollPx]` (one screenshot),
`GENESIS_DESKTOP_SHEET=usage|model|…` (open a `/` window once the agent is up),
`GENESIS_DESKTOP_AUTOSEND="task"`, `GENESIS_EXE=<path>` (which agent to start).
