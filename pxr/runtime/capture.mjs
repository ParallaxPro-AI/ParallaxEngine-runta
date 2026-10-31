import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const [url, output] = process.argv.slice(2);
const chrome = spawn('google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
  '--use-gl=swiftshader', '--enable-unsafe-swiftshader',
  '--remote-debugging-port=9333', '--window-size=1440,900',
  '--user-data-dir=/tmp/pxr-capture', 'about:blank',
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const deadline = Date.now() + 120_000;
let socket;

try {
  let targets;
  while (!targets && Date.now() < deadline) {
    try {
      targets = await (await fetch('http://127.0.0.1:9333/json')).json();
    } catch {
      await sleep(200);
    }
  }
  const page = targets?.find((target) => target.type === 'page');
  if (!page) throw new Error('Chrome did not open a page');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  let id = 0;
  const pending = new Map();
  let pageError = null;
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.method === 'Runtime.exceptionThrown') pageError = data.params.exceptionDetails.text;
    if (data.id && pending.has(data.id)) {
      pending.get(data.id)(data);
      pending.delete(data.id);
    }
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const commandId = ++id;
    const timer = setTimeout(() => {
      pending.delete(commandId);
      reject(new Error(`${method} timed out`));
    }, 30_000);
    pending.set(commandId, (response) => {
      clearTimeout(timer);
      response.error ? reject(new Error(response.error.message)) : resolve(response);
    });
    socket.send(JSON.stringify({ id: commandId, method, params }));
  });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url });
  let ready = false;
  while (!ready && Date.now() < deadline) {
    const result = await send('Runtime.evaluate', { expression: 'Boolean(globalThis.parallaxEditor)' });
    ready = result.result.result.value === true;
    if (!ready) await sleep(500);
  }
  if (!ready) throw new Error('editor did not load in Chrome');
  await send('Runtime.evaluate', { expression: 'globalThis.parallaxEditor.play()' });
  await sleep(3000);
  const screenshot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(output, Buffer.from(screenshot.result.data, 'base64'));
  if (pageError) throw new Error(pageError);
} finally {
  socket?.close();
  chrome.kill();
}
