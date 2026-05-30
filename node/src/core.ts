/**
 * 알리오 코어 모듈 (alio_core.py의 TypeScript 포팅)
 *
 * 알리오(www.alio.go.kr) 항목별공시 API 호출·파일 다운로드·HTML 파싱의
 * 순수 함수 모음. Python 원본(alio_core.py)과 동작 1:1 대응.
 *
 * 주의: MCP stdio 서버는 stdout이 JSON-RPC 채널이므로,
 *       이 모듈의 모든 로깅은 반드시 console.error(stderr)로만 한다.
 */
import { mkdir, writeFile, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import * as path from "node:path";

// ─────────────────────────────────────────────────────────
// 알리오 사이트 상수
// ─────────────────────────────────────────────────────────

export const BASE_URL = "https://www.alio.go.kr";

const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/131.0.0.0 Safari/537.36";

/** JSON POST 호출용 공통 헤더 */
export const JSON_HEADERS: Record<string, string> = {
  "Content-Type": "application/json;charset=UTF-8",
  "X-Requested-With": "XMLHttpRequest",
};

/** 내부규정 분류 코드 (alio_core.RULE_DIVIS_CODES) */
export const RULE_DIVIS_CODES: Record<string, string> = {
  전체: "",
  정관: "K1500",
  "인사·복무·징계": "K1100",
  보수: "K1200",
  직제: "K1300",
  기타: "K1400",
};

/** 첨부파일 다운로드 엔드포인트 통합 레지스트리 (alio_core.ENDPOINT_REGISTRY) */
const ENDPOINT_REGISTRY: Record<string, string> = {
  pdf: "/download/pdf.json",
  file: "/download/file.json",
  dfile: "/download/dfile.json",
  rule: "/download/rulefiledown.json",
};

// ─────────────────────────────────────────────────────────
// 파일명 정제 (sanitize_filename 포팅)
// ─────────────────────────────────────────────────────────

const INVALID_CHARS = /[<>:"/\\|?*\x00-\x1f]/g;
const WHITESPACE_RUN = /\s+/g;

/** Python str.strip(". ") — 양끝에서 '.'과 ' ' 문자 제거. */
function stripDotSpace(s: string): string {
  let start = 0;
  let end = s.length;
  while (start < end && (s[start] === "." || s[start] === " ")) start++;
  while (end > start && (s[end - 1] === "." || s[end - 1] === " ")) end--;
  return s.slice(start, end);
}

export function sanitizeFilename(name: string, maxLen = 80): string {
  if (!name) return "untitled";
  let s = name.replace(INVALID_CHARS, "");
  s = s.replace(WHITESPACE_RUN, " ").trim();
  s = stripDotSpace(s);
  if (!s) return "untitled";
  const ext = path.extname(s);
  const base = s.slice(0, s.length - ext.length);
  let remaining = maxLen - ext.length;
  if (remaining < 1) remaining = 1;
  return base.slice(0, remaining) + ext;
}

// ─────────────────────────────────────────────────────────
// 데이터 파싱 유틸 (parse_files_field 포팅)
// ─────────────────────────────────────────────────────────

export interface ParsedFile {
  id: string;
  name: string;
}

/** "101@파일명.pdf|102@파일명2.pdf" → [{id, name}, ...] */
export function parseFilesField(filesStr: string | null | undefined): ParsedFile[] {
  if (!filesStr) return [];
  const result: ParsedFile[] = [];
  for (let part of filesStr.split("|")) {
    part = part.trim();
    const at = part.indexOf("@");
    if (at !== -1) {
      result.push({ id: part.slice(0, at).trim(), name: part.slice(at + 1).trim() });
    } else if (part) {
      result.push({ id: "", name: part });
    }
  }
  return result;
}

// ─────────────────────────────────────────────────────────
// 네트워크 유틸리티 (create_session + retry_request 통합)
// ─────────────────────────────────────────────────────────

const RETRIABLE_STATUS = new Set([429, 500, 502, 503, 504]);

export interface RetryOptions {
  method?: string;
  /** JSON body. 지정 시 JSON.stringify 후 Content-Type 자동 설정. */
  jsonBody?: unknown;
  /** 쿼리 파라미터 (GET 등). */
  searchParams?: Record<string, string>;
  headers?: Record<string, string>;
  /** 전체 요청 타임아웃(ms). Python의 (connect, read) 튜플을 단일 타임아웃으로 단순화. */
  timeoutMs?: number;
  maxRetries?: number;
  /** backoff 기준(초). Python 기본 1.0. */
  backoff?: number;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * retry_request 포팅. 429/5xx 재시도(exponential backoff + jitter),
 * 429는 Retry-After 헤더 우선. 네트워크 예외도 동일하게 재시도.
 */
export async function retryFetch(url: string, opts: RetryOptions = {}): Promise<Response> {
  const {
    method = "GET",
    jsonBody,
    searchParams,
    headers = {},
    timeoutMs = 30000,
    maxRetries = 3,
    backoff = 1.0,
  } = opts;

  let target = url;
  if (searchParams) {
    const qs = new URLSearchParams(searchParams).toString();
    if (qs) target += (url.includes("?") ? "&" : "?") + qs;
  }

  const reqHeaders: Record<string, string> = {
    "User-Agent": DEFAULT_USER_AGENT,
    ...headers,
  };
  let body: string | undefined;
  if (jsonBody !== undefined) {
    body = JSON.stringify(jsonBody);
    if (!Object.keys(reqHeaders).some((h) => h.toLowerCase() === "content-type")) {
      reqHeaders["Content-Type"] = "application/json;charset=UTF-8";
    }
  }

  let lastErr: unknown;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await fetch(target, { method, headers: reqHeaders, body, signal: ctrl.signal });
      clearTimeout(timer);
      if (!RETRIABLE_STATUS.has(resp.status)) return resp;

      let wait: number;
      if (resp.status === 429) {
        const ra = resp.headers.get("Retry-After");
        const raTrim = ra !== null ? ra.trim() : "";
        const raNum = raTrim !== "" ? Number(raTrim) : NaN;
        // 빈 헤더·음수·NaN이면 backoff 폴백 (Python time.sleep 음수 ValueError 방지)
        wait = Number.isFinite(raNum) && raNum >= 0 ? raNum : backoff * 2 ** attempt;
      } else {
        wait = backoff * 2 ** attempt;
      }
      if (attempt < maxRetries) {
        const jitter = Math.random() * (wait * 0.5);
        await sleep(Math.max(0, (wait + jitter) * 1000));
        continue;
      }
      return resp;
    } catch (err) {
      clearTimeout(timer);
      lastErr = err;
      if (attempt < maxRetries) {
        const wait = backoff * 2 ** attempt;
        const jitter = Math.random() * (wait * 0.5);
        await sleep((wait + jitter) * 1000);
      } else {
        throw err;
      }
    }
  }
  throw lastErr;
}

/** retryFetch 후 JSON 파싱까지 한 번에. 비200/비JSON이면 throw. */
export async function fetchJson(url: string, opts: RetryOptions = {}): Promise<any> {
  const resp = await retryFetch(url, opts);
  if (resp.status !== 200) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ─────────────────────────────────────────────────────────
// 동시성 제한 헬퍼 (Python ThreadPoolExecutor(max_workers=N) 대응)
// ─────────────────────────────────────────────────────────

async function runLimited<T>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<void>
): Promise<void> {
  if (items.length === 0) return;
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const idx = cursor++;
      await fn(items[idx]);
    }
  });
  await Promise.all(workers);
}

// ─────────────────────────────────────────────────────────
// 파일 다운로드 (download_file_to_path 포팅)
// ─────────────────────────────────────────────────────────

export interface DownloadResult {
  success: boolean;
  savedPath: string;
  message: string;
}

/** 동일 파일명 충돌 시 (1), (2), ... 부여 (_resolve_collision_path 포팅). */
function resolveCollisionPath(target: string): string {
  if (!existsSync(target)) return target;
  const ext = path.extname(target);
  const base = target.slice(0, target.length - ext.length);
  let n = 1;
  while (existsSync(`${base}(${n})${ext}`)) n++;
  return `${base}(${n})${ext}`;
}

/**
 * 파일 단일 다운로드. JSON 응답이면 API 에러로 간주(파일 저장 안 함).
 * download_file_to_path 포팅.
 */
export async function downloadFileToPath(
  url: string,
  savePath: string,
  opts: { searchParams?: Record<string, string>; timeoutMs?: number } = {}
): Promise<DownloadResult> {
  try {
    const resp = await retryFetch(url, {
      method: "GET",
      searchParams: opts.searchParams,
      // 다운로드 기본 타임아웃 120s — 대용량 PDF/zip 저속 회선 대비
      // (Python requests의 read-timeout은 청크당 리셋이라 단일 데드라인인 fetch보다 관대)
      timeoutMs: opts.timeoutMs ?? 120000,
    });
    if (resp.status !== 200) return { success: false, savedPath: "", message: `HTTP ${resp.status}` };

    const ct = (resp.headers.get("content-type") ?? "").toLowerCase();
    if (ct.includes("json")) {
      let message = "unknown";
      try {
        const err: any = JSON.parse(await resp.text());
        message = err?.message || "unknown";
      } catch {
        /* JSON 파싱 실패 — Python은 이 경우 파일로 저장 시도하나,
           ct=json인데 비-JSON 바디는 실무상 알리오 에러 응답이므로 에러로 통일. */
      }
      return { success: false, savedPath: "", message: `API error: ${message}` };
    }

    const target = resolveCollisionPath(savePath);
    const buf = Buffer.from(await resp.arrayBuffer());
    await writeFile(target, buf);
    return { success: true, savedPath: target, message: "OK" };
  } catch (err: any) {
    return { success: false, savedPath: "", message: String(err?.message ?? err) };
  }
}

// ─────────────────────────────────────────────────────────
// 알리오 공시항목 자동 수집 (fetch_alio_items 포팅)
// ─────────────────────────────────────────────────────────

/**
 * 알리오 항목별공시 전체 메뉴를 formList.json에서 가져온다.
 * 반환: 항목 리스트 (실패 시 빈 리스트).
 */
export async function fetchAlioItems(): Promise<any[]> {
  const url = `${BASE_URL}/item/formList.json`;
  const headers = {
    "Content-Type": "application/json;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    Referer: `${BASE_URL}/item/itemList.do`,
  };
  try {
    const resp = await retryFetch(url, { method: "POST", jsonBody: {}, headers, timeoutMs: 30000 });
    if (resp.status !== 200) return [];
    const data: any = await resp.json();
    if (data?.status !== "success") return [];
    return data?.data ?? [];
  } catch (err) {
    console.error(`항목 메뉴 조회 실패: ${err}`);
    return [];
  }
}

// ─────────────────────────────────────────────────────────
// 게시판형 첨부파일 (HTML 파싱)
// ─────────────────────────────────────────────────────────

export interface ViolationMeta {
  report_form_no?: string;
  disclosure_no?: string;
  idx?: string;
  table_name?: string;
  idx_name?: string;
  bid_type?: string;
}

export interface BoardAttachment {
  kind: "upload" | "fileno";
  spath?: string;
  sfile?: string;
  dfile?: string;
  file_no?: string;
}

export interface ExternalLink {
  url: string;
  text: string;
}

// 패턴 A: downAttachFile('spath', 'sfile', 'dfile')
const PAT_DOWNATTACH =
  /downAttachFile\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]/g;
// 패턴 B: <a href="/download/download.json?fileNo=N">파일명</a>
const PAT_FILENO =
  /<a[^>]*\bhref=["'][^"']*?\/download\/download\.json\?fileNo=(\d+)["'][^>]*>([^<]+)<\/a>/g;
// 외부 링크
const PAT_EXTERNAL = /<a[^>]*\bhref=["'](https?:\/\/[^"']+)["'][^>]*>([^<]*)<\/a>/g;

function boardParams(apbaId: string, meta: ViolationMeta, rfn: string): Record<string, string> {
  return {
    disclosureNo: meta.disclosure_no ?? "",
    apbaId,
    nowcode: rfn,
    reportFormNo: rfn,
    table_name: meta.table_name ?? "",
    idx_name: meta.idx_name ?? "",
    idx: meta.idx ?? "",
    reportGbn: "N",
    bid_type: meta.bid_type ?? "",
  };
}

/**
 * 게시판형 자료의 첨부파일 메타를 itemBoard{reportFormNo}.do HTML에서 추출.
 * fetch_board_attachment_list 포팅 (패턴 A: upload, 패턴 B: fileno).
 */
export async function fetchBoardAttachmentList(
  apbaId: string,
  meta: ViolationMeta
): Promise<BoardAttachment[]> {
  const rfn = (meta.report_form_no ?? "").trim();
  if (!rfn) return [];
  const url = `${BASE_URL}/item/itemBoard${rfn}.do`;
  try {
    const resp = await retryFetch(url, {
      method: "GET",
      searchParams: boardParams(apbaId, meta, rfn),
      timeoutMs: 30000,
    });
    if (resp.status !== 200) return [];
    const text = await resp.text();
    const attachments: BoardAttachment[] = [];
    const seen = new Set<string>();

    for (const m of text.matchAll(PAT_DOWNATTACH)) {
      const [, spath, sfile, dfile] = m;
      const key = JSON.stringify(["upload", spath, sfile]);
      if (seen.has(key)) continue;
      seen.add(key);
      attachments.push({ kind: "upload", spath, sfile, dfile });
    }

    for (const m of text.matchAll(PAT_FILENO)) {
      const fileNo = m[1];
      const fileName = m[2].trim();
      const key = `fileno ${fileNo}`;
      if (seen.has(key)) continue;
      seen.add(key);
      attachments.push({ kind: "fileno", file_no: fileNo, dfile: fileName });
    }

    return attachments;
  } catch {
    return [];
  }
}

/**
 * 게시판형 자료의 외부 링크 추출 (입찰공고 g2b.go.kr 등).
 * fetch_board_external_links 포팅.
 */
export async function fetchBoardExternalLinks(
  apbaId: string,
  meta: ViolationMeta
): Promise<ExternalLink[]> {
  const rfn = (meta.report_form_no ?? "").trim();
  if (!rfn) return [];
  const url = `${BASE_URL}/item/itemBoard${rfn}.do`;
  try {
    const resp = await retryFetch(url, {
      method: "GET",
      searchParams: boardParams(apbaId, meta, rfn),
      timeoutMs: 15000,
    });
    if (resp.status !== 200) return [];
    const text = await resp.text();
    const seen = new Set<string>();
    const external: ExternalLink[] = [];
    for (const m of text.matchAll(PAT_EXTERNAL)) {
      const link = m[1].replace(/&amp;/g, "&");
      if (link.includes("alio.go.kr") || seen.has(link)) continue;
      seen.add(link);
      external.push({ url: link, text: m[2].trim().slice(0, 80) });
    }
    return external;
  } catch {
    return [];
  }
}

/**
 * 게시판형 첨부파일 1건 다운로드 (download_board_attachment 포팅).
 * kind="upload": /upload{spath}{sfile}, kind="fileno": /download/download.json?fileNo=N
 */
export async function downloadBoardAttachment(
  attachment: BoardAttachment,
  saveDir: string
): Promise<DownloadResult> {
  const kind = (attachment.kind ?? "upload").trim();
  const dfile = (attachment.dfile ?? "").trim();

  if (kind === "upload") {
    let spath = (attachment.spath ?? "").trim();
    const sfile = (attachment.sfile ?? "").trim();
    if (!spath || !sfile) return { success: false, savedPath: "", message: "missing spath/sfile" };
    if (!spath.startsWith("/")) spath = "/" + spath;
    const url = `${BASE_URL}/upload${spath}${sfile}`;
    const savePath = path.join(saveDir, sanitizeFilename(dfile || sfile, 120));
    return downloadFileToPath(url, savePath);
  }

  if (kind === "fileno") {
    const fileNo = (attachment.file_no ?? "").trim();
    if (!fileNo) return { success: false, savedPath: "", message: "missing file_no" };
    const url = `${BASE_URL}/download/download.json?fileNo=${fileNo}`;
    const savePath = path.join(saveDir, sanitizeFilename(dfile || `file_${fileNo}`, 120));
    return downloadFileToPath(url, savePath);
  }

  return { success: false, savedPath: "", message: `unknown kind: ${kind}` };
}

/**
 * 통합 첨부파일 다운로드 (download_attachment 포팅).
 * kind: "pdf" | "file" | "dfile" | "rule"
 */
export async function downloadAttachment(
  kind: string,
  fileInfo: { id?: string; name?: string },
  saveDir: string,
  disclosureNo = "",
  submissionNo = ""
): Promise<DownloadResult> {
  const base = ENDPOINT_REGISTRY[kind];
  if (!base) return { success: false, savedPath: "", message: `unknown kind: ${kind}` };

  const url = `${BASE_URL}${base}`;
  const fileName = fileInfo.name ?? "untitled";
  const savePath = path.join(saveDir, sanitizeFilename(fileName, 120));

  let params: Record<string, string>;
  if (kind === "pdf") {
    params = { disclosureNo };
  } else if (kind === "file") {
    params = { f: fileInfo.id ?? "", d: disclosureNo };
  } else if (kind === "dfile") {
    params = { fileName, submissionNo };
  } else if (kind === "rule") {
    params = { fileNo: fileInfo.id ?? "" };
  } else {
    return { success: false, savedPath: "", message: `unsupported kind: ${kind}` };
  }

  return downloadFileToPath(url, savePath, { searchParams: params });
}

// ─────────────────────────────────────────────────────────
// 공공기관 목록 (load_public_institutions 포팅)
// ─────────────────────────────────────────────────────────

export interface Institution {
  apba_id: string;
  inst_type: string;
  dept: string;
  region: string;
}

function addOrgans(dict: Map<string, Institution>, list: any[]): void {
  for (const item of list ?? []) {
    const name: string = item?.apbaNa ?? "";
    if (name) {
      dict.set(name, {
        apba_id: item?.apbaId ?? "",
        inst_type: item?.typeNa ?? "",
        dept: item?.jidtNa ?? "",
        region: item?.addrCd ?? "",
      });
    }
  }
}

/**
 * ALIO 기관목록 API에서 기관 목록 로드 (지역 정보 포함).
 * 1페이지로 totalPage 파악 후 2~N페이지를 동시성 5로 병렬 수집.
 * 반환: Map<기관명, Institution>. 실패 시 빈 Map.
 */
export async function loadPublicInstitutions(): Promise<Map<string, Institution>> {
  const url = `${BASE_URL}/organ/findOrganApbaList.json`;
  const makeBody = (pageNo: number) => ({
    apbaType: [],
    jidtDptm: [],
    area: [],
    apba_id: "",
    pageNo,
  });

  const dict = new Map<string, Institution>();
  try {
    const resp = await retryFetch(url, {
      method: "POST",
      jsonBody: makeBody(1),
      headers: JSON_HEADERS,
      timeoutMs: 30000,
    });
    if (resp.status !== 200) return dict;

    const data: any = await resp.json();
    const totalPage: number = data?.data?.organList?.page?.totalPage ?? 1;
    addOrgans(dict, data?.data?.organList?.result ?? []);

    if (totalPage <= 1) return dict;

    const pages: number[] = [];
    for (let p = 2; p <= totalPage; p++) pages.push(p);

    await runLimited(pages, 5, async (pageNo) => {
      try {
        const r = await retryFetch(url, {
          method: "POST",
          jsonBody: makeBody(pageNo),
          headers: JSON_HEADERS,
          timeoutMs: 30000,
        });
        if (r.status === 200) {
          const d: any = await r.json();
          addOrgans(dict, d?.data?.organList?.result ?? []);
        }
      } catch {
        /* 개별 페이지 실패는 무시 (Python과 동일) */
      }
    });

    return dict;
  } catch (err) {
    console.error(`공공기관 목록 로드 실패: ${err}`);
    return dict;
  }
}

// ─────────────────────────────────────────────────────────
// 내부규정 (findRuleList → findRuleDtl → rulefiledown)
// ─────────────────────────────────────────────────────────

export interface RuleListResult {
  totalCnt: number;
  result: any[];
  error?: string;
}

/** 기관명으로 내부규정 목록 1페이지 조회 (fetch_rule_list 포팅). */
export async function fetchRuleList(
  instName: string,
  divis = "",
  page = 1
): Promise<RuleListResult> {
  const url = `${BASE_URL}/occasional/findRuleList.json`;
  const params = { type: "apbaNa", word: instName, pageNo: String(page), divis };
  try {
    const resp = await retryFetch(url, { method: "GET", searchParams: params, timeoutMs: 30000 });
    if (resp.status !== 200) return { error: `HTTP ${resp.status}`, totalCnt: 0, result: [] };
    const data: any = ((await resp.json()) as any)?.data ?? {};
    return { totalCnt: data?.totalCnt ?? 0, result: data?.result ?? [] };
  } catch (err: any) {
    return { error: String(err?.message ?? err), totalCnt: 0, result: [] };
  }
}

/** 기관 내부규정 전체 페이지 자동 순회 (fetch_all_rules 포팅). */
export async function fetchAllRules(instName: string, divis = ""): Promise<any[]> {
  const first = await fetchRuleList(instName, divis, 1);
  const items: any[] = [...first.result];
  const totalCnt = first.totalCnt ?? 0;
  if (totalCnt <= items.length || items.length === 0) return items;
  const pageSize = Math.max(items.length, 1);
  const totalPage = Math.floor((totalCnt + pageSize - 1) / pageSize);
  for (let p = 2; p <= totalPage; p++) {
    const more = await fetchRuleList(instName, divis, p);
    items.push(...more.result);
  }
  return items;
}

export interface RuleFile {
  file_no: string;
  file_name: string;
}

export interface RuleDetail {
  seq: string;
  bFiles_raw: string;
  files: RuleFile[];
  latest: RuleFile | null;
  error?: string;
}

/**
 * 규정 상세 조회. bFiles에서 .zip 제외하고 fileNo가 가장 큰(최신) 파일 메타 반환.
 * fetch_rule_detail 포팅.
 */
export async function fetchRuleDetail(seq: string): Promise<RuleDetail> {
  const url = `${BASE_URL}/occasional/findRuleDtl.json`;
  try {
    const resp = await retryFetch(url, {
      method: "GET",
      searchParams: { seq },
      timeoutMs: 15000,
    });
    if (resp.status !== 200)
      return { error: `HTTP ${resp.status}`, seq, bFiles_raw: "", files: [], latest: null };
    const data: any = ((await resp.json()) as any)?.data ?? {};
    const bFiles: string = data?.bFiles ?? "";
    const files: RuleFile[] = [];
    for (const entryRaw of bFiles.split(",")) {
      const entry = entryRaw.trim();
      const bar = entry.indexOf("|");
      if (bar === -1) continue;
      const fileNo = entry.slice(0, bar).trim();
      const fileName = entry.slice(bar + 1).trim();
      if (fileName.toLowerCase().endsWith(".zip")) continue;
      files.push({ file_no: fileNo, file_name: fileName });
    }
    let latest: RuleFile | null = null;
    if (files.length) {
      // 10진 정수만 인정 (Python int()에 맞춤 — 0x1A·1e3·0b101 등은 비정수로 폴백)
      const allNumeric = files.every((f) => /^[+-]?\d+$/.test(f.file_no.trim()));
      if (allNumeric) {
        latest = files.reduce((a, b) => (Number(b.file_no) > Number(a.file_no) ? b : a));
      } else {
        latest = files[files.length - 1];
      }
    }
    return { seq, bFiles_raw: bFiles, files, latest };
  } catch (err: any) {
    return { error: String(err?.message ?? err), seq, bFiles_raw: "", files: [], latest: null };
  }
}

/** fileNo로 내부규정 파일 단건 다운로드 (download_rule_file_to_path 포팅). */
export async function downloadRuleFileToPath(
  fileNo: string,
  savePath: string
): Promise<DownloadResult> {
  const url = `${BASE_URL}/download/rulefiledown.json`;
  return downloadFileToPath(url, savePath, { searchParams: { fileNo } });
}

// ─────────────────────────────────────────────────────────
// 게시판형·보고서 자료 전체 페이지 자동 순회 (fetch_all_board_items 포팅)
// ─────────────────────────────────────────────────────────

export interface BoardItem {
  제목: string | null;
  등록일: string | null;
  기관ID: string | null;
  공시번호: string | null;
  제출번호: string | null;
  idx: string | null;
  reportFormNo: string | null;
  tableName: string | null;
  idxName: string | null;
  bidType: string | null;
}

export function toBoardItem(v: any): BoardItem {
  return {
    제목: v?.title ?? null,
    등록일: v?.idate ?? null,
    기관ID: v?.apbaId ?? null,
    공시번호: v?.disclosureNo ?? null,
    제출번호: v?.submissionNo ?? null,
    idx: v?.idx ?? null,
    reportFormNo: v?.reportFormNo ?? null,
    tableName: v?.tableName ?? null,
    idxName: v?.idxName ?? null,
    bidType: v?.bidType ?? null,
  };
}

/**
 * itemReportListSusi.json을 페이지 자동 순회로 모든 자료 반환.
 * fetch_all_board_items 포팅. 실패 시 빈 리스트.
 */
export async function fetchAllBoardItems(
  rootNo: string,
  apbaId = "",
  apbaType = ""
): Promise<BoardItem[]> {
  const url = `${BASE_URL}/item/itemReportListSusi.json`;
  const headers = { "Content-Type": "application/json;charset=UTF-8" };
  const items: BoardItem[] = [];
  let pageNo = 1;
  let totalPage = 1;

  while (pageNo <= totalPage) {
    const body = {
      pageNo,
      apbaId,
      apbaType,
      reportFormRootNo: rootNo,
      search_word: "",
      search_flag: "title",
      bid_type: "",
      enfc_istt: "",
    };
    try {
      const resp = await retryFetch(url, { method: "POST", jsonBody: body, headers, timeoutMs: 30000 });
      if (resp.status !== 200) break;
      const data: any = ((await resp.json()) as any)?.data ?? {};
      totalPage = data?.page?.totalPage ?? 1;
      if (!totalPage) totalPage = 1;
      for (const v of data?.result ?? []) items.push(toBoardItem(v));
    } catch {
      break;
    }
    pageNo += 1;
  }

  return items;
}

// ─────────────────────────────────────────────────────────
// 다운로드 공통 헬퍼 (도구 계층에서 사용)
// ─────────────────────────────────────────────────────────

/** save_dir 생성 + 다운로드 결과의 size_bytes 계산 헬퍼. */
export async function ensureDir(dir: string): Promise<void> {
  await mkdir(dir, { recursive: true });
}

export async function fileSize(p: string): Promise<number> {
  try {
    return (await stat(p)).size;
  } catch {
    return 0;
  }
}
