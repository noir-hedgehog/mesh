// Copyright (c) 2026-present Mesh contributors
// SPDX-License-Identifier: AGPL-3.0-only
// Requires SSH administrator access. Sessions expire in 10 minutes and are revoked in finally.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: {
  host: { type: "string", default: "ubuntu" },
  url: { type: "string", default: "http://100.79.187.62:8080" },
  workspace: { type: "string", default: "agentpm" },
  project: { type: "string" },
  issue: { type: "string" },
  "controls-issue": { type: "string" },
  output: { type: "string", default: ".agentpm/console-smoke" },
} });
for (const key of ["project", "issue"]) {
  assert.match(values[key] ?? "", /^[a-f0-9-]{36}$/, `${key} must be a UUID`);
}
if (values["controls-issue"]) assert.match(values["controls-issue"], /^[a-f0-9-]{36}$/);
assert.match(values.workspace, /^[a-zA-Z0-9_-]+$/);
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = values.url.replace(/\/$/, "");
const output = resolve(values.output);
mkdirSync(output, { recursive: true, mode: 0o700 });

function django(code) {
  try {
    return execFileSync("ssh", [values.host,
      "cd /opt/apps/mesh/current && sudo docker compose -f plane/docker-compose.yml exec -T api python manage.py shell"],
    { input: code, encoding: "utf8", timeout: 60000, stdio: ["pipe", "pipe", "pipe"] });
  } catch {
    throw new Error("Privileged smoke session operation failed; output suppressed to protect credentials");
  }
}

let session;
let browser;
const report = { pages: [], runtime: false, controls: false, screenshots: [], pageErrors: 0 };
try {
  const result = django(`
import json
from importlib import import_module
from django.conf import settings
from plane.db.models import ProjectMember
member = ProjectMember.objects.filter(project_id=${JSON.stringify(values.project)}, role=20, member__is_bot=False, member__is_active=True, deleted_at__isnull=True).select_related('member').first()
assert member, 'No active human project Admin'
user = member.member
store = import_module(settings.SESSION_ENGINE).SessionStore()
store['_auth_user_id'] = str(user.pk)
store['_auth_user_backend'] = settings.AUTHENTICATION_BACKENDS[0]
store['_auth_user_hash'] = user.get_session_auth_hash()
store.set_expiry(600)
store.save()
print('MESH_SESSION=' + json.dumps({'name': settings.SESSION_COOKIE_NAME, 'value': store.session_key}))
`);
  session = JSON.parse(result.split("\n").find((line) => line.startsWith("MESH_SESSION=")).slice(13));
  browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--no-proxy-server"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([{ ...session, url: base, httpOnly: true, sameSite: "Lax" }]);
  const page = await context.newPage();
  page.on("pageerror", () => report.pageErrors++);
  const issueUrl = (id) => `${base}/${values.workspace}/projects/${values.project}/issues/${id}/`;
  await page.goto(issueUrl(values.issue), { waitUntil: "domcontentloaded" });
  const details = page.locator("summary", { hasText: "Execution details" });
  await details.waitFor({ timeout: 45000 });
  await details.click();
  const executions = page.locator('section[aria-label$=" execution"]');
  assert.equal(await executions.count(), 3, "Acceptance run must have three stages");
  const executionText = (await executions.allTextContents()).join("\n");
  for (const model of ["kimi-for-coding", "MiniMax-M3", "MiniMax-M2.7"]) assert.ok(executionText.includes(model));
  await executions.first().locator("summary").first().click();
  await page.screenshot({ path: resolve(output, "runtime-desktop.png"), fullPage: true });
  report.screenshots.push("runtime-desktop.png");
  report.runtime = true;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: resolve(output, "runtime-mobile.png"), fullPage: true });
  report.screenshots.push("runtime-mobile.png");
  await page.setViewportSize({ width: 1440, height: 1000 });

  for (const section of ["members", "mesh/policy", "mesh/skills", "mesh/knowledge", "mesh/loops"]) {
    const endpoint = section === "members" ? "roles" : section.split("/")[1];
    const apiResponse = page.waitForResponse((response) =>
      response.url().includes(`/projects/${values.project}/mesh/${endpoint}/`) && response.ok(), { timeout: 30000 });
    await page.goto(`${base}/${values.workspace}/settings/projects/${values.project}/${section}/`, { waitUntil: "domcontentloaded" });
    await apiResponse;
    assert.ok(!page.url().includes("sign-in"), "Authenticated route redirected to sign-in");
    report.pages.push(section);
  }

  await page.goto(`${base}/${values.workspace}/settings/members/`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Add Agent", exact: true }).click({ timeout: 30000 });
  await page.getByRole("button", { name: /Iris agent:iris/ }).click({ timeout: 30000 });
  assert.ok((await page.getByLabel("A2A endpoint", { exact: true }).inputValue()).includes("/agents/iris/"));
  assert.equal(await page.getByLabel("New secret reference", { exact: true }).inputValue(), "");
  await page.getByRole("button", { name: "Sync Agent Card", exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, "agent-profile.png"), fullPage: true });
  report.screenshots.push("agent-profile.png");
  report.pages.push("agent-profile (read only)");

  if (values["controls-issue"]) {
    await page.goto(issueUrl(values["controls-issue"]), { waitUntil: "domcontentloaded" });
    const start = page.getByRole("button", { name: "Start Loop", exact: true });
    await start.waitFor({ timeout: 30000 });
    const started = page.waitForResponse((response) => response.url().endsWith("/start/") && response.request().method() === "POST");
    await start.click();
    const startResponse = await started;
    assert.ok(startResponse.ok(), "Start Loop failed");
    const startedPayload = await startResponse.json();
    assert.equal(startedPayload.run.status, "waiting_for_assignee");
    const canceled = page.waitForResponse((response) => response.url().endsWith("/cancel/") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Cancel Loop", exact: true }).click();
    const cancelResponse = await canceled;
    assert.ok(cancelResponse.ok(), "Cancel Loop failed");
    assert.equal((await cancelResponse.json()).run.status, "canceled");
    report.controls = true;
  }
  assert.equal(report.pageErrors, 0, "Browser encountered uncaught errors");
  console.log(JSON.stringify(report, null, 2));
} finally {
  if (browser) await browser.close();
  if (session) {
    django(`from importlib import import_module\nfrom django.conf import settings\nimport_module(settings.SESSION_ENGINE).SessionStore(session_key=${JSON.stringify(session.value)}).delete()\n`);
  }
}
