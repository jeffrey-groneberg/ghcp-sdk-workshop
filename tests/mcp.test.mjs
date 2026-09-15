import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));

class McpClient {
  constructor(directory) {
    this.pending = new Map();
    this.nextId = 0;
    this.stderr = "";
    this.child = spawn(process.execPath, [
      path.join(root, "node_modules/@playwright/mcp/cli.js"),
      "--config", path.join(root, "playwright-mcp.config.json"),
      "--output-dir", directory,
    ], {
      cwd: directory,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, COPILOT_GITHUB_TOKEN: "", GH_TOKEN: "", GITHUB_TOKEN: "" },
    });
    this.child.stderr.on("data", (data) => { this.stderr += data.toString(); });
    createInterface({ input: this.child.stdout }).on("line", (line) => {
      const message = JSON.parse(line);
      if (message.method === "roots/list") {
        this.send({ id: message.id, result: { roots: [{ uri: pathToFileURL(directory).href, name: "workshop-test" }] } });
        return;
      }
      const request = this.pending.get(message.id);
      if (request) {
        clearTimeout(request.timer);
        this.pending.delete(message.id);
        if (message.error) request.reject(new Error(JSON.stringify(message.error)));
        else request.resolve(message.result);
      }
    });
    this.child.on("error", (error) => this.fail(error));
    this.child.on("exit", (code) => this.fail(new Error(`MCP exited (${code}): ${this.stderr}`)));
  }

  fail(error) {
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    this.pending.clear();
  }

  send(message) {
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`);
  }

  request(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP timeout: ${method}\n${this.stderr}`));
      }, 30000);
      this.pending.set(id, { resolve, reject, timer });
      this.send({ id, method, params });
    });
  }

  async tool(name, args = {}) {
    const result = await this.request("tools/call", { name, arguments: args });
    assert.notEqual(result.isError, true, JSON.stringify(result));
    return result;
  }

  async close() {
    if (this.child.exitCode !== null || this.child.signalCode !== null) return;
    const exited = once(this.child, "exit");
    this.child.stdin.end();
    const timer = setTimeout(() => this.child.kill("SIGTERM"), 3000);
    await exited;
    clearTimeout(timer);
  }
}

test("official MCP captures a real full-page PNG in its assigned workspace", { timeout: 60000 }, async (t) => {
  const directory = await mkdtemp(path.join(tmpdir(), "copilot-workshop-mcp-"));
  let client;
  let server;
  t.after(async () => {
    if (client) await client.close();
    if (server?.listening) {
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
    await rm(directory, { recursive: true, force: true });
  });
  const page = (await readFile(path.join(root, "demo/index.html"), "utf8"))
    .replace("</style>", "body { min-height: 1800px; }</style>");
  server = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(page);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  client = new McpClient(directory);
  await client.request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: { roots: { listChanged: false } },
    clientInfo: { name: "workshop-test", version: "1.0.0" },
  });
  client.send({ method: "notifications/initialized" });
  const { tools } = await client.request("tools/list");
  for (const name of ["browser_navigate", "browser_snapshot", "browser_wait_for", "browser_take_screenshot"]) {
    assert.ok(tools.some((tool) => tool.name === name), `${name} is available`);
  }
  await client.tool("browser_navigate", { url: `http://127.0.0.1:${server.address().port}/` });
  await client.tool("browser_wait_for", { text: "Starter plan" });
  const snapshot = await client.tool("browser_snapshot");
  assert.match(JSON.stringify(snapshot.content), /Starter plan/);
  const capture = await client.tool("browser_take_screenshot", {
    filename: "after.png", fullPage: true, type: "png", scale: "css",
  });
  assert.ok(capture.content.every((part) => part.type !== "image"), "view, not MCP, supplies image input");
  const image = await readFile(path.join(directory, "after.png"));
  assert.deepEqual(image.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  assert.equal(image.readUInt32BE(16), 1280);
  assert.ok(image.readUInt32BE(20) >= 1800, "the screenshot includes content below the viewport");
});
