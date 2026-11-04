---
name: electron-app-bug-hunting
description: End-to-end methodology for finding vulnerabilities in ElectronJS desktop apps, from quick wins (DevTools, outdated deps, insecure flags) to chains like XSS to RCE, preload/IPC abuse, deep-link traversal and update hijack.
---

# ElectronJS App Bug Hunting

This skill gives you a repeatable, end-to-end methodology for security testing Electron desktop applications: confirm the target is Electron, extract the code, audit it statically, inspect it at runtime, validate exploit chains with safe proofs, rate severity and report.

## When to Use This Skill

Use this skill when the user:

- Asks about "ElectronJS app vulnerabilities" or "Electron pentest"
- Says "find me vulnerabilities in this Electron app" or hands over an `app.asar`, installer, or extracted Electron source
- Has identified, during desktop app research, that the target ships `app.asar`, an `Electron Framework` bundle, or an `Electron/x.y.z` user agent
- Wants to review `webPreferences`, preload scripts, IPC handlers, custom protocols or the auto-updater of a desktop app

## Ground Rules

- Only test apps that are in scope (bug bounty program, client engagement, or open-source software you run locally). Follow the program's rules on local-only findings.
- Never modify live installs or production update servers.
- Prove impact with **benign** actions only: `console.log`, reading `process.versions`, reading a file you created, or `whoami`. No destructive commands, no persistence.
- Always ask: **can a remote attacker deliver the input?** This decides severity more than anything else.

## Electron Mental Model (keep in mind)

| Component | Runs as | Why it matters |
|---|---|---|
| Main process (`package.json` → `main`) | Node.js, full OS privileges | Registers IPC handlers, protocols, updater, spawns processes |
| Renderer (`BrowserWindow`, `BrowserView`/`WebContentsView`, `<webview>`) | Chromium | Where XSS lands; power depends on `webPreferences` |
| Preload script | Privileged isolated context before page scripts | Exposes APIs via `contextBridge`; the main bridge from XSS to native |
| IPC (`ipcRenderer.send/invoke` → `ipcMain.on/handle`) | Renderer → main | Renderer without Node can still ask main to do privileged work |
| `app.asar` | Packed (not encrypted) archive | Full JS/HTML source for static analysis |
| Auto-updater | Main | Signature checks, staging paths, feed URLs |
| Native modules (`.node`) | Native code in process | Memory-safety bugs, outdated libs |

### Version-aware defaults (check before calling a missing flag a finding)

| Flag | Secure default since | Missing flag is a finding if Electron is... |
|---|---|---|
| `nodeIntegration: false` | Electron 5 | ≤ 4 |
| `contextIsolation: true` | Electron 12 | ≤ 11 |
| `sandbox: true` (renderer) | Electron 20 (unless `nodeIntegration: true` or `sandbox: false`) | ≤ 19 |
| `remote` module | Removed in Electron 14 | ≤ 13 with `enableRemoteModule` unset/true; on newer, look for `@electron/remote` |

---

## How to Use This Skill

### Step 1: Confirm It Is Electron and Get the Version

Locate the install and fingerprint it:

| OS | Typical location | Electron indicators |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Programs\<App>\`, `C:\Program Files\<App>\` | `resources\app.asar`, `LICENSES.chromium.html`, `chrome_100_percent.pak` |
| macOS | `/Applications/<App>.app/Contents/` | `Resources/app.asar`, `Frameworks/Electron Framework.framework` |
| Linux | `/opt/<App>/`, `/usr/lib/<app>/`, AppImage mount | `resources/app.asar`, `chrome-sandbox` |

Get the Electron and Chromium versions (they drive every later decision):

```bash
# From the binary
strings -a "<App binary>" | grep -m1 -oE "Electron/[0-9.]+"
strings -a "<App binary>" | grep -m1 -oE "Chrome/[0-9.]+"

# macOS
plutil -p "/Applications/App.app/Contents/Frameworks/Electron Framework.framework/Resources/Info.plist" | grep -i version

# Read Electron fuses (RunAsNode, inspect args, ASAR integrity, OnlyLoadAppFromAsar)
npx @electron/fuses read --app "<path to App binary or .app>"
```

Record: app version, Electron version, Chromium version, fuse states, platform.

### Step 2: Extract and Triage the Source

```bash
npm install -g @electron/asar
asar extract app.asar ./src      # also copy app.asar.unpacked/ if present
cd ./src && cat package.json     # note "main", dependencies, build config
```

- Follow `main` to the entry file and map every file it `require`s / `import`s.
- If code is bundled/minified, beautify it (`npx prettier --write "**/*.js"`) and look for `*.map` source maps. Shipped source maps often give back original source.
- Identify the frontend framework (React, Vue, Angular) and the markdown/HTML renderer — these are your XSS sinks.
- Inventory native modules: `find . -name "*.node"`.

### Step 3: Quick Wins (do these first, 15 minutes)

1. **Dependencies:**
   ```bash
   npm i --package-lock-only   # only if package-lock.json is missing
   npm audit
   ```
   Keep only findings in packages actually loaded at runtime and reachable with attacker input. A vulnerable dev-only package is not a bug.
2. **Outdated Electron/Chromium:** compare the Chromium version with current stable. Old engines carry public renderer/V8 bugs. Report only with a realistic delivery path; otherwise note it as hardening.
3. **DevTools in production:** check the View menu for "Toggle Developer Tools", try `Ctrl+Shift+I` / `Ctrl+Shift+J` / `F12` / `Cmd+Opt+I`, right-click for "Inspect".
4. **Debug flags in code:**
   ```bash
   rg -n "openDevTools|devTools\s*:\s*true|electron-is-dev|isPackaged|remote-debugging-port|ELECTRON_ENABLE_LOGGING|--no-sandbox|disable-web-security|ignore-certificate-errors" .
   ```
5. **Hardcoded secrets and endpoints:**
   ```bash
   rg -n -i "api[_-]?key|secret|token|password|bearer|aws_|private_key" .
   rg -n -o "https?://[a-zA-Z0-9./_-]+" . | sort -u   # build an endpoint list for backend testing
   ```

### Step 4: Static Audit of the Electron Attack Surface

Work through each area. Each one lists what to search and what counts as a finding.

#### 4.1 Window and renderer configuration

```bash
rg -n "new BrowserWindow|new BrowserView|WebContentsView|webPreferences|<webview|webviewTag|allowpopups" .
rg -n "nodeIntegration|nodeIntegrationInSubFrames|nodeIntegrationInWorker|contextIsolation|sandbox|webSecurity|allowRunningInsecureContent|enableRemoteModule|@electron/remote|experimentalFeatures|enableBlinkFeatures" .
```

Audit **every** window, view and `<webview>` separately — one weak secondary window (help, preview, OAuth popup) is enough. Finding if:
- `nodeIntegration: true`, `contextIsolation: false`, or `sandbox: false` on a window that loads remote or attacker-influenced content
- `webSecurity: false` or `allowRunningInsecureContent: true`
- `<webview>` with `allowpopups`, `nodeintegration`, or a weaker preload
- The window loads `http://` content or a remote origin you can influence

#### 4.2 Preload scripts and `contextBridge`

```bash
rg -n "preload\s*:" .
rg -n "exposeInMainWorld|window\.\w+\s*=" <preload files>
rg -n "child_process|exec|spawn|fs\.|require\(|eval\(|new Function|executeJavaScript|ipcRenderer\.(send|invoke|on)" <preload files>
```

Finding if the bridge exposes:
- Command execution, arbitrary file read/write, or `require` of caller-controlled modules
- A raw `ipcRenderer` (or a generic `send(channel, data)` / `invoke(channel, ...)`) that lets page JS reach **any** IPC channel
- Functions that take paths, URLs or code strings without validation

#### 4.3 IPC map (renderer → main)

```bash
rg -n "ipcMain\.(on|handle|once)\(" .
rg -n "ipcRenderer\.(send|invoke|sendSync)\(|webContents\.send\(" .
```

Build a table: channel → argument shape → what the main handler does → is `event.sender` / `event.senderFrame.url` validated → is input validated. Finding if a handler:
- Passes input to `exec`/`spawn`/`execFile`, `fs.*`, `require`, `shell.*`, `loadURL`, or a downloader
- Does not check which frame/origin sent the message (iframes and `<webview>`s can send IPC too)
- Uses input in path joins without `path.resolve` + `startsWith(allowedDir)`

#### 4.4 XSS sinks in the renderer

```bash
rg -n "innerHTML|outerHTML|insertAdjacentHTML|document\.write|dangerouslySetInnerHTML|v-html|bypassSecurityTrust|executeJavaScript|eval\(|new Function|setTimeout\(\s*['\"]" .
rg -n "marked|markdown-it|showdown|remark|DOMPurify|sanitize-html" .
rg -n "Content-Security-Policy|http-equiv" .
```

Trace from **remote-controllable sources** (synced notes/messages, shared documents, filenames, opened files, profile fields, API responses, deep-link parameters) to these sinks. Note whether a CSP exists and whether it allows `unsafe-inline`/`unsafe-eval`.

#### 4.5 Navigation and new windows

```bash
rg -n "will-navigate|will-redirect|setWindowOpenHandler|new-window|did-create-window" .
```

Finding if there is no `will-navigate` restriction or no `setWindowOpenHandler` deny/allowlist — a link or `window.open` can move a privileged renderer (with its preload) to an attacker origin.

#### 4.6 External openers

```bash
rg -n "shell\.(openExternal|openPath|showItemInFolder)" .
```

Finding if attacker-influenced URLs reach `openExternal` without scheme + host allowlisting. `file:`, UNC (`\\host\share`), `smb:` and OS protocol handlers (e.g. `ms-*:` on Windows) can lead to code execution or NTLM leaks.

#### 4.7 Custom protocols and deep links

```bash
rg -n "setAsDefaultProtocolClient|protocol\.(handle|register\w*Protocol|registerSchemesAsPrivileged)|open-url|second-instance|process\.argv" .
```

Deep links are remotely triggerable from any web page, which makes them high value. Finding if the handler:
- Joins query/path into filesystem paths without normalization (traversal → file read/write)
- Passes values into `loadURL`, `openExternal`, IPC, or command lines
- Parses `process.argv` on `second-instance` and trusts flags (argument injection, e.g. Chromium switches)
- Maps custom schemes to `file:` or serves files outside the app directory

#### 4.8 Auto-update and supply chain

```bash
rg -n "autoUpdater|electron-updater|update-electron-app|Squirrel|setFeedURL|latest\.yml|publish" .
```

Check: HTTPS feed, signature/hash verification (`verifyUpdateCodeSignature`, publisher name checks), downgrade protection, and whether staging/temp directories are predictable or writable by other users.

#### 4.9 Main-process network, local servers and permissions

```bash
rg -n "net\.request|fetch\(|axios|got\(|http\.request|https\.request" <main files>
rg -n "createServer|listen\(|WebSocketServer|ws\b" .
rg -n "setPermissionRequestHandler|setPermissionCheckHandler|getUserMedia|desktopCapturer" .
rg -n "safeStorage|keytar|localStorage\.setItem|electron-store" .
```

Finding if:
- Main fetches renderer-supplied URLs (SSRF to localhost/internal/metadata)
- A local HTTP/WebSocket server has no auth token or `Origin` check (any website can reach `127.0.0.1`)
- No permission handler (camera/mic/screen granted to any origin)
- Tokens stored in plaintext instead of `safeStorage`/OS keychain

### Step 5: Runtime Inspection

#### 5.1 Get DevTools even if the menu is hidden

```bash
"<App binary>" --remote-debugging-port=9222   # then open chrome://inspect in Chrome
"<App binary>" --inspect=9229                 # main-process Node debugger, if the fuse allows it
```

If blocked, patch the extracted source to call `openDevTools()`, repack with `asar pack ./src app.asar`, and replace (lab only; ASAR integrity fuses may refuse modified archives).

#### 5.2 Probe the renderer (lab, DevTools console)

```js
navigator.userAgent                                  // Electron/Chrome versions
typeof require, typeof process, typeof module        // Node exposed?
Object.keys(window).filter(k => !(k in document.createElement('iframe').contentWindow ?? {}))  // injected globals
// For each exposed bridge object, list its shape without calling dangerous members:
Object.entries(window.api ?? {}).map(([k, v]) => [k, typeof v])
```

From the main process (if you can attach), dump effective settings: `webContents.getAllWebContents().map(w => [w.getURL(), w.getLastWebPreferences()])`.

#### 5.3 Logs, process and filesystem behaviour

```bash
# Capture output (run the real binary, not the shortcut)
"<App binary>" > app-log.txt 2>&1
grep -iE "tmp|temp|update|\.zip|\.tar|download|preload|devtools|token|http://" app-log.txt

# Process flags and children (Linux/macOS)
ps -o pid,cmd -p <PID>;  pstree -p <PID>;  lsof -p <PID>
```

- Windows: Procmon filtered on the process, watching `%TEMP%`, `%LOCALAPPDATA%`, registry Run keys, and DLL loads from writable paths.
- macOS: `sudo fs_usage -w -f filesys <PID>`; check `codesign -dv --verbose=4` and entitlements.
- Linux: `inotifywait -m /tmp -e create,modify,delete`.
- Look for predictable update/temp files, world-writable directories in the path chain, and unquoted Windows service/Run-key paths.

#### 5.4 Local data at rest

Inspect the app data directory: `%APPDATA%\<App>\`, `~/Library/Application Support/<App>/`, `~/.config/<App>/`.

```bash
rg -a -i "token|refresh|bearer|password|session" "<app data dir>"   # Local Storage/leveldb, IndexedDB, config.json, logs
sqlite3 "<app data dir>/Cookies" "select host_key,name,is_httponly,is_secure from cookies;"
```

Plaintext long-lived tokens and secrets in logs are valid findings, but usually low severity (local attacker model).

### Step 6: Intercept Traffic and Test the Backend

1. Install the Burp/mitmproxy CA in the OS trust store.
2. Set the system proxy, or launch with `"<App binary>" --proxy-server="http://127.0.0.1:8080"`.
3. Watch for WebSocket upgrades — many apps move all traffic to WS after login.
4. If certificate pinning blocks you: hook the check with Frida, or patch the pinning logic (e.g. `certificate-error` handler, `setCertificateVerifyProc`) in the extracted source and repack.
5. Test the API like any web/mobile backend: IDOR/BOLA, auth flaws, SQLi, SSRF, mass assignment. Use the endpoint list from Step 3; desktop-only endpoints are often less tested.

### Step 7: Validate Exploit Chains (safe proofs)

Confirm each candidate with the smallest benign proof, then document the chain.

| Chain | Precondition | Safe proof |
|---|---|---|
| XSS → Node RCE | `nodeIntegration: true` (or ≤ v4 default) on a window rendering attacker content | Payload logs `require('os').userInfo().username` or runs `whoami` |
| XSS → preload bridge | Bridge exposes exec/fs/raw IPC | Call the exposed function to read a file you created |
| XSS → IPC → main | Handler does privileged work on input, no sender check | Send the crafted message to read/write a test file in your lab directory |
| `contextIsolation: false` → privileged globals | Preload attaches objects to `window` or Electron internals reachable | Show page JS reaching the preload's privileged object |
| Navigation escape | No `will-navigate` / `setWindowOpenHandler` | `<a href="https://your-domain" target="_blank">` opens your page inside the app; show which preload/APIs it gets |
| Deep link → file read/write/RCE | Unvalidated protocol handler | Trigger `myapp://...` from a web page you host; read a test file via traversal |
| `openExternal` abuse | Attacker URL reaches `openExternal` | Open `file:` path to a harmless binary like `calc.exe` in lab, or a UNC path to your SMB listener (NTLM capture) |
| Update hijack | HTTP feed / no signature / writable staging | Show unsigned artifact accepted in lab with your own update server |
| Main-process SSRF | Main fetches renderer-supplied URL | Request to your server or `127.0.0.1` service returns in the app |
| Local server CSRF/WS hijack | No token/Origin check | A web page you host calls the local endpoint |

Start XSS testing with harmless payloads (`<img src=x onerror=console.log(document.domain)>`, `<svg onload=console.log(1)>`) in every rich-content field: notes, chat, markdown preview, filenames, imported files, profile fields, notifications.

### Step 8: Rate Severity

| Finding | Typical severity |
|---|---|
| Remote input → RCE (XSS + Node/bridge/IPC, deep link RCE, `openExternal` to executable) | Critical |
| Deep link file read/write, update hijack via MITM, main-process SSRF to internal services | High |
| XSS without native reach (token theft, account actions), WS/local server hijack from web | Medium–High |
| Navigation/popup to arbitrary origin without further impact | Low–Medium (keep as a chain gadget) |
| DevTools enabled, plaintext local tokens, insecure fuses (`RunAsNode`, inspect args), outdated Electron without a working chain | Low / Informational (local attacker) |

Adjust for user interaction (0-click > 1-click > multi-step) and check program rules: many bug bounty programs exclude local-only issues.

### Step 9: Report

For each finding include:
- App version, Electron/Chromium version, OS
- Vulnerable code location (file, line from the extracted source) and the effective `webPreferences`
- Delivery vector (how a remote attacker gets input in) and step-by-step reproduction
- Benign PoC evidence (screenshot, video, log)
- Impact and severity reasoning
- Remediation from the baseline below

---

## Secure Baseline (use for remediation advice)

- Every window/view/`<webview>`: `sandbox: true`, `contextIsolation: true`, `nodeIntegration: false`, `webSecurity: true`, `allowRunningInsecureContent: false`; no `remote` / `@electron/remote`.
- Deny navigation by default (`will-navigate`), `setWindowOpenHandler(() => ({ action: 'deny' }))`, allowlist known origins only.
- Preload exposes small, high-level, parameterized functions; never `ipcRenderer`, `fs`, `child_process` or `eval`.
- Every IPC handler validates `event.senderFrame` origin and checks arguments against a schema.
- `shell.openExternal` only through a helper enforcing `https:` and allowlisted hosts.
- Protocol handlers canonicalize input, reject `..`, `file:` and UNC, and stay inside an allowlisted directory.
- Strict CSP without `unsafe-inline`/`unsafe-eval`; sanitize HTML (DOMPurify); avoid `innerHTML`/`executeJavaScript`.
- Signed updates over HTTPS with hash verification and user-only, randomized staging.
- Secrets in `safeStorage`/OS keychain; cookies `HttpOnly`, `Secure`, `SameSite`.
- `session.setPermissionRequestHandler` deny-by-default.
- Production builds: no DevTools, no `--remote-debugging-port`, no `--no-sandbox`/`--disable-web-security`; fuses `RunAsNode` and `EnableNodeCliInspectArguments` off, ASAR integrity on.
- Keep Electron/Chromium close to current stable.

## Further Reading

- DeepStrike — Penetration Testing of Electron-based Applications: https://deepstrike.io/blog/penetration-testing-of-electron-based-applications
- Electron security checklist: https://www.electronjs.org/docs/latest/tutorial/security
- Carettoni, "Preloading Insecurity in Your Electron" (Black Hat Asia 2019): https://i.blackhat.com/asia-19/Thu-March-28/bh-asia-Carettoni-Preloading-Insecurity-In-Your-Electron-wp.pdf
- NDSS 2023, security study of Electron applications: https://www.ndss-symposium.org/wp-content/uploads/2023/02/ndss2023_f305_paper.pdf
- Real audit reports for pattern-hunting: Obsidian (Cure53, 2023), Bitwarden desktop (2023)
- Electronegativity (Doyensec) for automated misconfiguration scanning: `npx @doyensec/electronegativity -i ./src`
