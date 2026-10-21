#!/usr/bin/env node
// alio-mcp 설치기 (Claude Code용)
// 최신 릴리스에서 서버·조사관 에이전트를 내려받아 Claude Code에 user 범위로 등록하고, 알리오 응답까지 점검한다.
// 사용: node install.mjs [--no-check] [--from <로컬 폴더>]
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const REPO = "giovinazo/alio-mcp";
const BASE = `https://github.com/${REPO}/releases/latest/download/`;
const IS_WIN = process.platform === "win32";
const HOME = os.homedir();
const INSTALL_DIR = path.join(HOME, ".alio-mcp");
const SERVER = path.join(INSTALL_DIR, "server", "index.mjs");
const AGENT = path.join(HOME, ".claude", "agents", "alio-investigator.md");

const argv = process.argv.slice(2);
const SKIP_CHECK = argv.includes("--no-check");
const fromIdx = argv.indexOf("--from");
const LOCAL = fromIdx >= 0 ? argv[fromIdx + 1] : ""; // 시험용: 내려받기 대신 로컬 폴더에서 복사

const ok = (m) => console.log(`  ✔ ${m}`);
const fail = (m) => { console.log(`  ✘ ${m}`); process.exit(1); };

// ── 1) 실행 환경 ─────────────────────────────────────────
console.log("[1/4] 실행 환경 확인");
const major = Number(process.versions.node.split(".")[0]);
if (major < 18) fail(`Node ${process.versions.node}: 18 이상이 필요합니다. https://nodejs.org 에서 LTS를 설치하세요.`);
ok(`Node ${process.versions.node}`);

function claude(args) {
  // 윈도우는 claude.cmd·claude.exe 둘 다 있을 수 있어 셸을 거쳐 부른다
  if (!IS_WIN) return spawnSync("claude", args, { encoding: "utf8" });
  const q = (a) => (/[\s"&|<>^]/.test(a) ? `"${a.replace(/"/g, '\\"')}"` : a);
  return spawnSync(["claude", ...args].map(q).join(" "), { encoding: "utf8", shell: true });
}
const ver = claude(["--version"]);
if (ver.status !== 0) fail("claude 명령을 찾지 못했습니다. Claude Code를 먼저 설치하세요.");
ok(`Claude Code ${ver.stdout.trim()}`);

// ── 2) 내려받기 ──────────────────────────────────────────
console.log("[2/4] 서버·조사관 내려받기");
async function get(name, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const tmp = `${dest}.part`;
  if (LOCAL) {
    fs.copyFileSync(path.join(LOCAL, name), tmp);
  } else {
    try {
      const res = await fetch(BASE + name);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      fs.writeFileSync(tmp, Buffer.from(await res.arrayBuffer()));
    } catch (e) {
      // 회사망 SSL 검사 장비 등으로 Node 내려받기가 막히면 운영체제 인증서를 쓰는 curl로 다시 시도
      const r = spawnSync(IS_WIN ? "curl.exe" : "curl", ["-fsSL", "-o", tmp, BASE + name], { encoding: "utf8" });
      if (r.status !== 0) fail(`${name} 내려받기 실패 (${e.message}; curl: ${(r.stderr || "").trim()})`);
    }
  }
  if (fs.statSync(tmp).size === 0) fail(`${name}: 빈 파일`);
  fs.renameSync(tmp, dest);
}
await get("alio-mcp-server.mjs", SERVER);
ok(`서버 → ${SERVER}`);

if (fs.existsSync(AGENT)) {
  const bak = `${AGENT}.${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.bak`;
  fs.copyFileSync(AGENT, bak);
  ok(`기존 조사관 정의 보관 → ${bak}`);
}
await get("alio-investigator.md", AGENT);
ok(`조사관 에이전트 → ${AGENT}`);

// ── 3) Claude Code 등록 ──────────────────────────────────
console.log("[3/4] Claude Code에 등록 (user 범위)");
claude(["mcp", "remove", "alio", "-s", "user"]); // 이전 등록이 있으면 지움, 없으면 무시
const nodeCmd = IS_WIN ? process.execPath : "node";
const add = claude(["mcp", "add", "alio", "-s", "user", "--", nodeCmd, SERVER]);
if (add.status !== 0) fail(`등록 실패: ${(add.stderr || add.stdout).trim()}`);
ok(`alio → ${nodeCmd} ${SERVER}`);

// ── 4) 자체 점검 ─────────────────────────────────────────
if (SKIP_CHECK) {
  console.log("[4/4] 점검 생략 (--no-check)");
} else {
  console.log("[4/4] 서버 기동·알리오 응답 점검 (최대 90초)");
  const res = await selfCheck();
  ok(`도구 ${res.tools}개 노출`);
  ok(`알리오 응답: '산업단지' 검색 → ${res.found}`);
}
console.log("\n설치 완료. Claude Code를 다시 시작한 뒤 '/mcp'에서 alio가 연결됨으로 보이면 됩니다.");
console.log("예: \"한국산업단지공단 내부규정 목록 보여줘\"");

function selfCheck() {
  return new Promise((resolve) => {
    const p = spawn(process.execPath, [SERVER], { stdio: ["pipe", "pipe", "ignore"] });
    const out = { tools: 0, found: "" };
    const timer = setTimeout(() => { p.kill(); fail("90초 안에 응답이 없습니다. 회사망이면 프록시·방화벽 설정을 확인하세요."); }, 90000);
    const send = (m) => p.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
    let buf = "";
    p.stdout.on("data", (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        let m; try { m = JSON.parse(line); } catch { continue; }
        if (m.id === 1) {
          send({ method: "notifications/initialized" });
          send({ id: 2, method: "tools/list" });
        } else if (m.id === 2) {
          out.tools = m.result?.tools?.length ?? 0;
          if (out.tools === 0) fail("도구 목록이 비어 있습니다.");
          send({ id: 3, method: "tools/call", params: { name: "search_organs", arguments: { name: "산업단지" } } });
        } else if (m.id === 3) {
          clearTimeout(timer); p.kill();
          const text = m.result?.content?.[0]?.text ?? "";
          if (m.result?.isError || !text.includes("한국산업단지공단")) fail(`알리오 응답 이상: ${text.slice(0, 200)}`);
          out.found = "한국산업단지공단 확인";
          resolve(out);
        }
      }
    });
    send({ id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "alio-install", version: "1" } } });
  });
}
