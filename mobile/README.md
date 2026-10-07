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

## iOS testers (Expo Go, no Apple Developer account)

The project lives in the Expo organization `metko777s-team`
(`eas init` set `owner` and `extra.eas.projectId` in `app.json`).
`ios.runtimeVersion` is `exposdk:57.0.0` so Expo Go can load the update.

```bash
npx eas-cli update --branch testers --platform ios --environment preview --message "..."
```

Testers: install **Expo Go** from the App Store, sign in with their own Expo
account, get invited to `metko777s-team` (expo.dev → Members), then scan the
QR code of the update on its EAS dashboard page. On iOS, Expo Go only opens
updates of projects the signed-in account owns or is a member of.
