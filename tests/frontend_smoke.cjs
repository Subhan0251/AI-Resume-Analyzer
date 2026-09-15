/* Browser integration checks using Node 22+ and an installed Chrome/Edge.
 * No npm dependencies or live model calls. Run: node tests/frontend_smoke.cjs
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { once } = require("node:events");

const root = path.resolve(__dirname, "..");
const artifacts = path.join(root, ".artifacts");
fs.mkdirSync(artifacts, { recursive: true });
const profile = fs.mkdtempSync(path.join(artifacts, "browser-"));
const browser = process.env.BROWSER_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].find(fs.existsSync);
if (!browser) throw new Error("Set BROWSER_PATH to an installed Chrome or Edge executable.");

let mode = "success", lastRequest = null, requests = 0;
const fixture = JSON.parse(fs.readFileSync(path.join(root, "frontend/sample-report.json"), "utf8"));
fixture.sample = false;
fixture.gaps[0].missing_skill = "<img src=x onerror=alert(1)>";
const { execFileSync } = require("node:child_process");
const pdfBytes = execFileSync(path.join(root, ".venv/Scripts/python.exe"), ["-c", "import json,sys;from reports import render_pdf;sys.stdout.buffer.write(render_pdf(json.load(open('frontend/sample-report.json',encoding='utf-8'))))"], {cwd:root});
const server = http.createServer(async (req, res) => {
  if (req.url === "/api/v1/report/pdf") { res.writeHead(200, {"Content-Type":"application/pdf"}); res.end(pdfBytes); return; }
  if (req.url === "/api/v1/analyze") {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    lastRequest = { headers: req.headers, body: Buffer.concat(chunks).toString() }; requests++;
    await new Promise(resolve => setTimeout(resolve, 150));
    const status = mode === "invalid" ? 422 : mode === "auth" ? 401 : mode === "limit" ? 429 : 200;
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(status === 200 ? fixture : { detail: "Please provide a valid job description containing the role's responsibilities." }));
    return;
  }
  const files = { "/": ["index.html", "text/html"], "/static/app.js": ["app.js", "text/javascript"], "/static/styles.css": ["styles.css", "text/css"], "/static/sample-report.json": ["sample-report.json", "application/json"] };
  const file = files[req.url];
  if (!file) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "Content-Type": file[1] });
  res.end(fs.readFileSync(path.join(root, "frontend", file[0])));
});

async function waitFor(fn, label, timeout = 12000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await fn()) return; await new Promise(r => setTimeout(r, 60)); }
  throw new Error(`Timed out: ${label}`);
}

(async () => {
  let child, ws, send;
  try {
    server.listen(0, "127.0.0.1"); await once(server, "listening");
    child = spawn(browser, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { windowsHide: true, stdio: "ignore" });
    await waitFor(() => fs.existsSync(path.join(profile, "DevToolsActivePort")), "browser startup");
    const port = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split(/\r?\n/)[0];
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    ws = new WebSocket(pages.find(p => p.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.addEventListener("open", resolve, { once: true }); ws.addEventListener("error", reject, { once: true }); });
    let id = 0;
    const pending = new Map(), errors = [];
    ws.addEventListener("message", event => {
      const message = JSON.parse(event.data);
      if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text);
      const callback = pending.get(message.id);
      if (callback) { pending.delete(message.id); message.error ? callback.reject(new Error(message.error.message)) : callback.resolve(message.result); }
    });
    send = (method, params = {}) => new Promise((resolve, reject) => {
      const callId = ++id;
      const timeout = setTimeout(() => { pending.delete(callId); reject(new Error(`CDP timeout: ${method}`)); }, 12000);
      pending.set(callId, { resolve: value => { clearTimeout(timeout); resolve(value); }, reject: error => { clearTimeout(timeout); reject(error); } });
      ws.send(JSON.stringify({ id: callId, method, params }));
    });
    const evaluate = async expression => {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
      return result.result.value;
    };
    const click = id => evaluate(`document.getElementById(${JSON.stringify(id)}).click()`);
    const screenshot = async name => {
      const image = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      fs.writeFileSync(path.join(artifacts, name), Buffer.from(image.data, "base64"));
    };
    await send("Runtime.enable"); await send("Page.enable");
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }, { name: "prefers-color-scheme", value: "dark" }] });
    await send("Page.navigate", { url: `http://127.0.0.1:${server.address().port}/` });
    await waitFor(() => evaluate("document.readyState === 'complete' && !!document.getElementById('sample-button')"), "workspace load");
    assert.equal(await evaluate("document.documentElement.dataset.theme"), "dark");
    assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
    await screenshot("frontend-dark.png");
    await click("theme-toggle");
    assert.equal(await evaluate("document.documentElement.dataset.theme"), "light");
    await screenshot("frontend-light.png");
    await send("Page.reload");
    await waitFor(() => evaluate("document.readyState === 'complete' && document.documentElement.dataset.theme === 'light'"), "theme persistence");
    await click("analyze-button");
    assert.equal(await evaluate("document.getElementById('file-error').hidden"), false);
    assert.equal(requests, 0);
    // Exercise the real file input handler with a browser File object.
    const selectFile = (name, content) => evaluate(`(() => {
      const transfer = new DataTransfer(); transfer.items.add(new File([${JSON.stringify(content)}], ${JSON.stringify(name)}));
      const input = document.getElementById('resume-file'); input.files = transfer.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await selectFile("bad.txt", "example");
    assert.match(await evaluate("document.getElementById('file-error').textContent"), /PDF or DOCX/);
    await selectFile("cv.pdf", "%PDF-synthetic");
    await evaluate("document.getElementById('job-description').value = 'string'; document.getElementById('job-description').dispatchEvent(new Event('input'));");
    await click("analyze-button");
    assert.equal(requests, 0);
    await evaluate("document.getElementById('job-description').value = 'Python developer: build APIs with SQL.'; document.getElementById('job-description').dispatchEvent(new Event('input'));");
    await click("analyze-button");
    assert.equal(await evaluate("document.getElementById('access-dialog').open"), true);
    await evaluate("document.getElementById('access-key').value = 'synthetic-access-key-for-browser-tests'; document.getElementById('access-form').requestSubmit();");
    mode = "invalid"; await click("analyze-button");
    assert.equal(await evaluate("document.getElementById('analyze-button').disabled"), true);
    await waitFor(() => evaluate("!document.getElementById('analyze-button').disabled"), "invalid JD response");
    assert.match(await evaluate("document.getElementById('request-error').textContent"), /valid job description/);
    assert.equal(await evaluate("document.getElementById('selected-file').hidden"), false);
    mode = "auth"; await click("analyze-button");
    await waitFor(() => evaluate("!document.getElementById('analyze-button').disabled"), "authentication error");
    assert.match(await evaluate("document.getElementById('request-error').textContent"), /access key wasn’t accepted/);
    mode = "limit"; await click("analyze-button");
    await waitFor(() => evaluate("!document.getElementById('analyze-button').disabled"), "rate limit response");
    assert.match(await evaluate("document.getElementById('request-error').textContent"), /request limit/);
    mode = "success"; await click("analyze-button");
    await waitFor(() => evaluate("!document.getElementById('analyze-button').disabled"), "successful response");
    assert.equal(lastRequest.headers["x-api-key"], "synthetic-access-key-for-browser-tests");
    assert.match(lastRequest.body, /name="job_description"/); assert.match(lastRequest.body, /filename="cv.pdf"/);
    assert.equal(await evaluate("document.getElementById('overall-score').textContent"), "25");
    assert.equal(await evaluate("document.querySelectorAll('#gap-list img').length"), 0);
    assert.match(await evaluate("document.getElementById('gap-list').textContent"), /<img src=x/);
    assert.equal(await evaluate("Object.keys(localStorage).length === 1 && localStorage.getItem('matchpoint-theme') === 'light'"), true);
    await click("insights-button");
    assert.equal(await evaluate("document.getElementById('insights-content').hidden"), false);
    assert.equal(await evaluate("document.querySelectorAll('.criterion-insight').length"), 11);
    assert.equal(await evaluate("document.querySelectorAll('#gap-list [hidden]').length"), 3);
    await click("all-gaps");
    assert.equal(await evaluate("document.querySelectorAll('#gap-list [hidden]').length"), 0);
    await click("sample-button");
    await waitFor(() => evaluate("!document.getElementById('sample-banner').hidden"), "sample report");
    assert.equal(await evaluate("document.getElementById('sample-banner').hidden"), false);
    await send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: artifacts });
    const download = path.join(artifacts, "matchpoint-sample.pdf");
    if (fs.existsSync(download)) fs.unlinkSync(download);
    await click("export-button");
    await waitFor(() => fs.existsSync(download), "report download");
    assert.equal(fs.readFileSync(download).subarray(0, 4).toString(), "%PDF");
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
    await screenshot("frontend-mobile-results.png");
    await click("reset-button");
    assert.equal(await evaluate("document.getElementById('job-description').value"), "");
    assert.equal(await evaluate("document.getElementById('results').hidden"), true);
    await evaluate("window.scrollTo(0, 0)"); await screenshot("frontend-mobile.png");
    await send("Page.reload");
    await waitFor(() => evaluate("document.readyState === 'complete' && !!document.getElementById('open-settings')"), "reload");
    await click("open-settings");
    assert.match(await evaluate("document.getElementById('access-status').textContent"), /No access key/);
    assert.deepEqual(errors, []);
    console.log("Browser checks passed: themes, mobile overflow, file/JD validation, access key, HTTP errors, multipart submission, safe results, sample report, export, reset and memory-only credentials.");
    console.log("Screenshots saved to .artifacts/.");
  } finally {
    if (send && ws?.readyState === WebSocket.OPEN) { try { await send("Browser.close"); } catch {} }
    if (ws) ws.close();
    if (child && child.exitCode === null) { await Promise.race([once(child, "exit"), new Promise(r => setTimeout(r, 2000))]); if (child.exitCode === null) child.kill(); }
    server.closeAllConnections(); server.close();
    // Only delete this test's generated browser profile, inside the project artifact directory.
    if (path.dirname(path.resolve(profile)) !== artifacts || !path.basename(profile).startsWith("browser-")) throw new Error("Unsafe browser cleanup path");
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
