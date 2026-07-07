#!/usr/bin/env node
import { spawn } from "node:child_process";
import { mkdir, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

class CdpSession {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.waiters = new Map();
    this.socket.addEventListener("message", (event) => this.onMessage(event));
    this.socket.addEventListener("close", () => this.onClose());
  }

  static async open(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    return new CdpSession(socket);
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }

  waitForEvent(method, timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const waiters = this.waiters.get(method) || [];
        this.waiters.set(
          method,
          waiters.filter((item) => item.resolve !== resolve),
        );
        reject(new Error(`timed out waiting for ${method}`));
      }, timeoutMs);
      const waiters = this.waiters.get(method) || [];
      waiters.push({
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
      });
      this.waiters.set(method, waiters);
    });
  }

  close() {
    this.socket.close();
  }

  onMessage(event) {
    const message = JSON.parse(event.data);
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) {
        return;
      }
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error.message || "CDP command failed"));
      } else {
        pending.resolve(message.result || {});
      }
      return;
    }
    if (message.method) {
      const waiters = this.waiters.get(message.method) || [];
      const waiter = waiters.shift();
      if (waiter) {
        waiter.resolve(message.params || {});
      }
      this.waiters.set(message.method, waiters);
    }
  }

  onClose() {
    for (const pending of this.pending.values()) {
      pending.reject(new Error("Chrome DevTools connection closed"));
    }
    this.pending.clear();
  }
}

const [, , browser, baseUrl, outDir] = process.argv;
const token = process.env.BRIGADE_TOKEN || "";

if (!browser || !baseUrl || !outDir) {
  console.error("usage: web-browser-smoke-cdp.mjs <browser> <base-url> <out-dir>");
  process.exit(2);
}
if (!token) {
  console.error("BRIGADE_TOKEN is required for authenticated browser smoke");
  process.exit(2);
}
if (typeof WebSocket === "undefined") {
  console.error("Node.js with a built-in WebSocket client is required");
  process.exit(2);
}

const port = await freePort();
const profileDir = path.join(outDir, `chrome-profile-${process.pid}`);
await mkdir(outDir, { recursive: true });
await mkdir(profileDir, { recursive: true });

const chrome = spawn(
  browser,
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--disable-background-networking",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "pipe"] },
);

let stderr = "";
chrome.stderr.setEncoding("utf-8");
chrome.stderr.on("data", (chunk) => {
  stderr += chunk;
});

let cdp;
try {
  const target = await createTarget(port);
  cdp = await CdpSession.open(target.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");

  await setViewport(cdp, 1440, 1000);
  await navigate(cdp, pageUrl("/?view=cockpit"));
  await cdp.send("Runtime.evaluate", {
    expression: `localStorage.setItem("brigade_token", ${JSON.stringify(token)});`,
    returnByValue: true,
  });

  await captureView(cdp, "/?view=cockpit", "1440,1000", "cockpit-dom.html", "cockpit-desktop.png", [
    "OpenBrigade",
    "Cockpit",
    "Task Queue",
  ]);
  await captureView(cdp, "/?view=ops", "1440,1000", "ops-dom.html", "ops-desktop.png", [
    "OpenBrigade",
    "Ops Room",
  ]);
  await captureView(cdp, "/?view=proposals", "1440,1000", "proposals-dom.html", "proposals-desktop.png", [
    "OpenBrigade",
    "Approval Workbench",
  ]);
  await captureView(cdp, "/?view=cockpit", "390,844", null, "cockpit-mobile.png", [
    "OpenBrigade",
    "Cockpit",
  ]);
} finally {
  if (cdp) {
    await cdp.send("Browser.close").catch(() => undefined);
    cdp.close();
  }
  if (!chrome.killed) {
    chrome.kill("SIGTERM");
  }
  await waitForExit(chrome).catch(() => undefined);
  await rm(profileDir, { recursive: true, force: true }).catch(() => undefined);
}

async function captureView(cdp, urlPath, size, domFile, screenshotFile, expectedText) {
  const [width, height] = size.split(",").map((item) => Number.parseInt(item, 10));
  await setViewport(cdp, width, height);
  await navigate(cdp, pageUrl(urlPath));
  await waitForText(cdp, expectedText);
  if (domFile) {
    const html = await evaluate(cdp, "document.documentElement.outerHTML");
    await writeFile(path.join(outDir, domFile), html, "utf-8");
  }
  const screenshot = await cdp.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  await writeFile(path.join(outDir, screenshotFile), Buffer.from(screenshot.data, "base64"));
}

async function navigate(cdp, url) {
  const loaded = cdp.waitForEvent("Page.loadEventFired", 15000).catch(() => undefined);
  await cdp.send("Page.navigate", { url });
  await loaded;
  await delay(500);
}

async function waitForText(cdp, expectedText) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const text = await evaluate(cdp, "document.body ? document.body.innerText : ''");
    if (expectedText.every((item) => text.includes(item))) {
      return;
    }
    await delay(250);
  }
  const text = await evaluate(cdp, "document.body ? document.body.innerText : ''");
  throw new Error(`page did not render expected text: ${expectedText.join(", ")}\n${text}`);
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "runtime evaluation failed");
  }
  return result.result.value ?? "";
}

async function setViewport(cdp, width, height) {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width < 700,
  });
}

function pageUrl(urlPath) {
  return new URL(urlPath, baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`).toString();
}

async function createTarget(port) {
  await waitForChrome(port);
  const response = await fetch(
    `http://127.0.0.1:${port}/json/new?${encodeURIComponent("about:blank")}`,
    { method: "PUT" },
  );
  if (!response.ok) {
    throw new Error(`could not create Chrome target: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function waitForChrome(port) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (chrome.exitCode !== null) {
      throw new Error(`Chrome exited before DevTools was ready\n${stderr}`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) {
        return;
      }
    } catch {
      // Chrome is still starting.
    }
    await delay(125);
  }
  throw new Error(`timed out waiting for Chrome DevTools\n${stderr}`);
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  await new Promise((resolve) => server.close(resolve));
  return address.port;
}

function waitForExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    child.once("exit", resolve);
  });
}
