# Real-browser checks (`webapp/frontend/tests/e2e/`)

Manual end-to-end scripts that drive a real Firefox against a running webapp. They are not part of
`node --test webapp/frontend/tests/*.test.mjs` (the glob does not reach this directory) or `python3 main.py -u`:
they need a server, a browser and the live database behind the server.

| File | Purpose |
| :--- | :--- |
| `webdriver_bidi.mjs` | Dependency-free client: `openBrowser()` (geckodriver session + WebDriver BiDi socket), `goto`, `refresh`, `evaluate(body)` in the page realm, `screenshot`; `APP_PRELUDE` (the app's own `state`, the build's module `base`, `wait`, `sleep`) and `BLOB_HELPER` |
| `nodemap_tab_e2e.mjs` | NodeMap tab nodes (`webapp/API_FRONTEND.md` §5.3.1, §5.3.4): keep-alive across tab switches, Keep changes / Frozen, reload restore, PNG export with live tabs (snapshot and 2D paint fallback), dark or light theme |

## Running

```bash
# 1. the webapp on a spare port (holds DB connections: run nothing else that opens the DB/tunnel meanwhile)
python3 webapp/main.py -c config.json --host 127.0.0.1 --port 8765 --no-reload &
# 2. geckodriver (Firefox's WebDriver; /snap/bin/geckodriver here)
geckodriver --port 4444 &
# 3. the check (BASE, WEBDRIVER, THEME=dark|light, OUT for the PNGs / screenshots)
THEME=dark OUT=/tmp/e2e node webapp/frontend/tests/e2e/nodemap_tab_e2e.mjs
```

Stop both background processes afterwards, before running the Python suite (`tests/AGENTS.md` §2).

## Things that bite

- **Evaluate in the page realm (BiDi), not with classic `execute/async`.** geckodriver runs classic scripts in a
  sandbox whose dynamic `import()` builds its own module graph: `state.openTab()` there changes a second `state`
  the app never renders. `evaluate()` uses BiDi `script.evaluate` for this reason.
- **Downloads**: the app's CSP blocks `fetch()` of `blob:` URLs. Capture the Blob by wrapping
  `URL.createObjectURL` (and stub `HTMLAnchorElement.prototype.click` for `download` links), then read it with
  `blobToBase64()`.
- **Controllers** are not global: patch a prototype method from the page (`NodeMapController.prototype.commit`)
  to capture the instance, as `nodemap_tab_e2e.mjs` does.
- **Chromium** under `~/.cache/ms-playwright` lacks system libraries on this machine (`libatk`), which is why the
  Playwright smoke tests skip; Firefox + geckodriver works.
- **Viewing the PNGs**: the Read guard blocks files over 40 KB; shrink first, e.g.
  `magick out.png -resize 1000x1000 -quality 55 out_s.jpg`.
