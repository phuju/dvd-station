# DiscStation mobile app

Expo (React Native) app that mirrors the appliance's built-in web UI
(`../src/static/`) and talks to the DiscStation Python host over plain HTTP.

## Requirements

- Node 18+ (this repo built with Node 22)
- The **Expo Go** app on your phone (Android or iOS)
- Phone, laptop and the DiscStation appliance on the **same Wi-Fi / LAN**

No Android SDK / Xcode / Java needed for Expo Go testing.

## Host side

`discstation.py` now serves plain HTTP for this app on **port 8081** alongside
the self-signed HTTPS web UI on 8080. Verify:

```
curl -s http://<appliance-ip>:8081/status      # -> READY
```

Change/disable the port with `DISCSTATION_HTTP_PORT` in the service env
(`DISCSTATION_HTTP_PORT=0` disables it).

## Run

```
cd mobile
npm install          # first time only
npx expo start
```

Scan the QR with Expo Go. In the app, open **⚙ settings**, enter the appliance
IP (e.g. `192.168.0.101` — port defaults to `8081`), tap **TEST CONNECTION**,
then **SAVE**.

## What it does (parity with the web UI)

| Web UI | App |
|---|---|
| `LINK: LIVE/OFFLINE` + `SYSTEM:` status | connection dot + footer stamp, polling `/progress` every 2 s |
| URL / PATH tab → `POST /` `url=` | same |
| UPLOAD FILES tab, folder+file picker, `_paths` JSON, `POST /` multipart | file picker only (no folder trees on mobile); `_paths` sent flat |
| DISC LABEL → `POST /set-label` | same |
| disc-capacity meter from `/disc-info` | same (shown once files are selected) |
| light / dark toggle | same (☾ / ☀), follows system by default |

Not ported: PWA install, service worker, drag-and-drop, `webkitdirectory`
folder trees.

## Responsive layout

The UI scales to any device — small phone, large phone, tablet, portrait or
landscape — and re-flows live on rotation:

- `src/responsive.ts` — `makeMetrics(width, height)` derives a font/spacing
  scale from the screen's **shortest side** (dampened so tablets get larger,
  not literally proportional, text).
- `App.tsx` uses `useWindowDimensions()` + `react-native-safe-area-context`
  (`useSafeAreaInsets`) — every size in `makeStyles` goes through `ms()` (type)
  / `sp()` (spacing); the content column is centred and capped
  (`contentMax`); the capability strip is 2 columns on phones, 4 on tablets;
  notch / status-bar / nav-bar insets are respected.

## Files

- `App.tsx` — the single screen + settings modal
- `src/theme.ts` — palette tokens ported from `style.css`
- `src/responsive.ts` — device-adaptive scale (`ms`, `sp`, breakpoints)
- `src/api.ts` — host HTTP client (`/progress`, `/disc-info`, `/`, `/set-label`)
- `src/storage.ts` — persisted host address + theme override (AsyncStorage)
