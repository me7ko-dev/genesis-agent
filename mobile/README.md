# Genesis Remote

The phone app for [Genesis](../README.md): Android, iOS and web from one Expo
codebase. The agent runs on your computer (`genesis serve`); this app pairs
with it by QR code and talks to it over an end-to-end encrypted channel.

User guide (Bulgarian): [docs/MOBILE.md](../docs/MOBILE.md).

```bash
npm ci
npm test            # protocol tests, including against the real Python server
npm run typecheck
npx expo start      # run in Expo Go
npm run xcode       # generate and open the Xcode project (macOS)
```
