/**
 * 알리오 코어 모듈 (alio_core.py의 TypeScript 포팅)
 *
 * 알리오(www.alio.go.kr) 항목별공시 API 호출·파일 다운로드·HTML 파싱의
 * 순수 함수 모음. Python 원본(alio_core.py)과 동작 1:1 대응.
 *
 * 주의: MCP stdio 서버는 stdout이 JSON-RPC 채널이므로,
 *       이 모듈의 모든 로깅은 반드시 console.error(stderr)로만 한다.
 */
import { mkdir, writeFile, stat, rename, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import * as path from "node:path";

// ─────────────────────────────────────────────────────────
// TLS 검증 정책 (Python alio_core.py의 create_session(verify=False) + 경고억제 대응)
// ─────────────────────────────────────────────────────────
// alio.go.kr는 일부 기관·기업 외부망의 SSL 검사(가로채기) 보안장비 뒤에 있어, 기본
// 인증서 검증을 켜면 "self-signed certificate in chain" 오류로 모든 요청이 실패한다.
// Python 코어와 동일하게 기본은 검증을 끄고(환경변수 ALIO_VERIFY_SSL=1 일 때만 검증),
// 검증을 끌 때 Node가 매번 출력하는 TLS 경고는 한 번만 정의해 숨긴다(= urllib3.disable_warnings).
const ALIO_VERIFY_SSL = process.env.ALIO_VERIFY_SSL === "1";
if (!ALIO_VERIFY_SSL) {
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
  const _emitWarning = process.emitWarning.bind(process);
  process.emitWarning = ((warning: unknown, ...rest: unknown[]) => {
    const msg = typeof warning === "string" ? warning : (warning as Error | undefined)?.message ?? "";
    if (typeof msg === "string" && msg.includes("NODE_TLS_REJECT_UNAUTHORIZED")) return;
    return (_emitWarning as (...a: unknown[]) => void)(warning, ...rest);
  }) as typeof process.emitWarning;
}

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

export async function runLimited<T>(
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
  opts: { searchParams?: Record<string, string>; timeoutMs?: number; maxRetries?: number } = {}
): Promise<DownloadResult> {
  // 본문 수신(arrayBuffer)은 retryFetch(요청 단계) 이후라, 스트리밍 중
  // 연결이 끊기면(ConnectionReset·타임아웃) retryFetch가 잡지 못한다.
  // 따라서 요청+본문수신 전체를 maxRetries만큼 재시도하고, 부분 파일이
  // 남지 않도록 .part에 받은 뒤 성공 시 원자적으로 교체한다.
  const maxRetries = opts.maxRetries ?? 3;
  const backoff = 1.0;
  const target = resolveCollisionPath(savePath);
  const tmp = `${target}.part`;
  let lastErr = "";
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const resp = await retryFetch(url, {
        method: "GET",
        searchParams: opts.searchParams,
        // 다운로드 기본 타임아웃 120s — 대용량 PDF/zip 저속 회선 대비
        timeoutMs: opts.timeoutMs ?? 120000,
        // 재시도 책임을 이 외부 루프로 일원화(maxRetries=0). 그러지 않으면 retryFetch
        // 내부 재시도(기본 3회)와 외부 루프가 곱해져 헤더 단계 오류 시 과도한 시도·
        // backoff 누적이 발생한다. 본문(arrayBuffer) 끊김은 외부 루프만 잡을 수 있다.
        maxRetries: 0,
      });
      if (resp.status !== 200) {
        lastErr = `HTTP ${resp.status}`;
        if (RETRIABLE_STATUS.has(resp.status) && attempt < maxRetries) {
          await sleep((backoff * 2 ** attempt + Math.random() * 0.5) * 1000);
          continue;
        }
        return { success: false, savedPath: "", message: lastErr };
      }

      const ct = (resp.headers.get("content-type") ?? "").toLowerCase();
      if (ct.includes("json")) {
        let message = "unknown";
        try {
          const err: any = JSON.parse(await resp.text());
          message = err?.message || "unknown";
        } catch {
          /* ct=json인데 비-JSON 바디는 실무상 알리오 에러 응답이므로 에러로 통일. */
        }
        return { success: false, savedPath: "", message: `API error: ${message}` };
      }

      const buf = Buffer.from(await resp.arrayBuffer());
      await writeFile(tmp, buf);
      await rename(tmp, target);
      return { success: true, savedPath: target, message: "OK" };
    } catch (err: any) {
      lastErr = String(err?.message ?? err);
      try {
        await rm(tmp, { force: true });
      } catch {
        /* 부분파일 정리 실패는 무시 */
      }
      if (attempt < maxRetries) {
        await sleep((backoff * 2 ** attempt + Math.random() * 0.5) * 1000);
        continue;
      }
      return { success: false, savedPath: "", message: lastErr };
    }
  }
  return { success: false, savedPath: "", message: lastErr };
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

// ─────────────────────────────────────────────────────────
// 메뉴 항목 유틸 (build_item_* / detect_endpoint_kind 포팅)
// ─────────────────────────────────────────────────────────

export function buildItemDisplayName(item: any): string {
  const scdnm = (item?.scdnm ?? "").trim();
  const mcdnm = (item?.mcdnm ?? "").trim();
  return scdnm || mcdnm || (item?.mcd ?? "(미상)");
}

export function buildItemRootNo(item: any): string {
  // Python `reportNos or mcd` — 빈 문자열도 falsy 처리(||)
  return String(item?.reportNos || item?.mcd || "").trim();
}

export function detectEndpointKind(item: any): string {
  const mcd = item?.mcd ?? "";
  if (mcd === "21110") return "rule";
  const reportNos = String(item?.reportNos ?? "");
  if (reportNos.includes("70401")) return "pdf+file+dfile";
  if (String(item?.reportYn ?? "").toUpperCase() === "Y") return "pdf+file";
  return "file";
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

// ─────────────────────────────────────────────────────────
// 보고서형 본문(HTML 표) 조회 — itemReportRight.do
// ─────────────────────────────────────────────────────────

/** HTML → 평문 (script/style 제거·태그 제거·엔티티 복원·공백 정규화). html_to_text 포팅. */
export function htmlToText(html: string): string {
  let s = html.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  s = s.replace(/<[^>]+>/g, " ");
  s = s
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
  return s.replace(/\s+/g, " ").trim();
}

/**
 * HTML <table>들을 행렬(string[][])로 추출. 빈 행·빈 표 제외. parse_html_tables 포팅.
 * itemReportRight.do는 Vue 레이아웃 table이 섞여 있어 표 경계가 부정확할 수 있으므로
 * 호출부는 본문 평문(htmlToText)을 항상 함께 제공해 보조한다.
 */
export function parseHtmlTables(html: string): string[][][] {
  const tables: string[][][] = [];
  const tableRe = /<table[^>]*>([\s\S]*?)<\/table>/gi;
  let tm: RegExpExecArray | null;
  while ((tm = tableRe.exec(html)) !== null) {
    const rows: string[][] = [];
    const trRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
    let rm: RegExpExecArray | null;
    while ((rm = trRe.exec(tm[1])) !== null) {
      const cells: string[] = [];
      const cellRe = /<t[hd][^>]*>([\s\S]*?)<\/t[hd]>/gi;
      let cm: RegExpExecArray | null;
      while ((cm = cellRe.exec(rm[1])) !== null) {
        cells.push(htmlToText(cm[1]));
      }
      if (cells.some((c) => c)) rows.push(cells);
    }
    if (rows.length > 0) tables.push(rows);
  }
  return tables;
}

/**
 * 보고서형 공시의 본문을 itemReportRight.do HTML에서 표·평문으로 추출.
 * fetch_report_tables 포팅. download_report(PDF 저장)와 달리 파일을 만들지 않고
 * 내용을 즉시 반환한다. 징계현황·임직원수·복리후생비 등 보고서형 항목의 실데이터를
 * PDF/HWP 변환 없이 반환한다. pdf.json이 "해당 사항이 없는 항목입니다" boilerplate를
 * 주는 항목(임직원수·임원연봉 등)도 이 HTML 경로는 실데이터다.
 */
export async function fetchReportTables(disclosureNo: string): Promise<any> {
  if (!disclosureNo) return { error: "MISSING: disclosureNo가 필수입니다" };
  const url = `${BASE_URL}/item/itemReportRight.do`;
  let resp: Response;
  try {
    resp = await retryFetch(url, {
      method: "GET",
      searchParams: { disclosureNo },
      timeoutMs: 30000,
    });
  } catch (err) {
    return { error: `REQUEST_FAILED: ${err}` };
  }
  if (resp.status !== 200) return { error: `REQUEST_FAILED: HTTP ${resp.status}` };

  const html = await resp.text();
  const tables = parseHtmlTables(html);
  const text = htmlToText(html);
  if (tables.length === 0 && !text) {
    return {
      error: `EMPTY: disclosureNo='${disclosureNo}' 본문 없음 (순수 첨부 항목일 수 있음 — download_* 도구 사용)`,
    };
  }
  return {
    disclosureNo,
    제목: text.slice(0, 60).trim(),
    표_개수: tables.length,
    표: tables,
    본문텍스트: text.slice(0, 4000),
  };
}

/**
 * 보고서형 공시의 부속 첨부 메타를 itemReportFiles.json에서 조회.
 * (fetch_disclosure_attachments 포팅)
 *
 * download_disclosure_attachment 호출에 필요한 식별자를 한 번에 제공:
 *   - fileNo       → kind='file'의 fileId
 *   - fileName     → kind='dfile'의 fileName (orcpFileNa)
 *   - submissionNo → kind='dfile'에 필요
 *
 * 반환: { disclosureNo, 첨부: [{fileNo, fileName, submissionNo, fileType, savePath}] } | { error }
 */
export async function fetchDisclosureAttachments(disclosureNo: string): Promise<any> {
  if (!disclosureNo) return { error: "MISSING: disclosureNo가 필수입니다" };
  const url = `${BASE_URL}/item/itemReportFiles.json`;
  let data: any[];
  try {
    const resp = await retryFetch(url, {
      method: "GET",
      searchParams: { disclosureNo },
      timeoutMs: 20000,
    });
    if (resp.status !== 200) return { error: `REQUEST_FAILED: HTTP ${resp.status}` };
    const body: any = await resp.json();
    data = body?.data ?? [];
  } catch (err) {
    return { error: `REQUEST_FAILED: ${err}` };
  }
  const 첨부 = (Array.isArray(data) ? data : [])
    .filter((f) => f && typeof f === "object")
    .map((f: any) => ({
      fileNo: String(f.fileNo ?? ""),
      fileName: f.orcpFileNa ?? f.saveFileNa ?? "",
      submissionNo: String(f.submissionNo ?? ""),
      fileType: f.fileType ?? "",
      savePath: f.savePath ?? "",
    }));
  return { disclosureNo, 첨부 };
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
// 기관 프로필 / 다중기관 disclosureNo / 정형 집계 (v1.3.0)
// ─────────────────────────────────────────────────────────

function cleanField(v: any): string {
  if (v === null || v === undefined) return "";
  const s = String(v).trim();
  return ["none", "null"].includes(s.toLowerCase()) ? "" : s;
}

export async function fetchOrganProfile(apbaId: string): Promise<any> {
  if (!apbaId) return { error: "MISSING: apba_id가 필수입니다" };
  const url = `${BASE_URL}/organ/findOrganApbaList.json`;
  const body = { apbaType: [], jidtDptm: [], area: [], apba_id: apbaId, pageNo: 1 };
  let result: any[];
  try {
    const resp = await retryFetch(url, {
      method: "POST",
      jsonBody: body,
      headers: { ...JSON_HEADERS, "X-Requested-With": "XMLHttpRequest" },
      timeoutMs: 30000,
    });
    if (resp.status !== 200) return { error: `REQUEST_FAILED: HTTP ${resp.status}` };
    const node = (((await resp.json()) as any)?.data ?? {})?.organList ?? {};
    result = Array.isArray(node) ? node : node?.result ?? [];
  } catch (e: any) {
    return { error: `REQUEST_FAILED: ${e?.message ?? e}` };
  }
  const match = (result ?? []).find((o: any) => o?.apbaId === apbaId);
  if (!match) return { error: `NOT_FOUND: apba_id='${apbaId}' 기관 없음` };
  return {
    기관ID: match.apbaId ?? "",
    기관명: match.apbaNa ?? "",
    기관유형: cleanField(match.typeNa),
    주무부처: cleanField(match.jidtNa),
    기관장: cleanField(match.ceo),
    홈페이지: cleanField(match.homepage),
    주소: cleanField(match.addr1),
    지역: cleanField(match.addrCd),
    설립일: cleanField(match.fdate),
    예산: cleanField(match.fmoney),
    소개: cleanField(match.contents).replace(/&cr;/g, "\n"),
    유튜브: cleanField(match.youtUrl),
    상위기관: cleanField(match.parnApbaNa),
    submissionNo: cleanField(match.submissionNo),
  };
}

export async function fetchOrganDisclosureMap(
  rootNo: string,
  apbaIds: string[]
): Promise<Record<string, { 기관명: string; disclosureNo: string; submissionNo: string }>> {
  const primary = (rootNo ?? "").split(",")[0].trim();
  if (!primary) return {};
  const want = new Set(apbaIds ?? []);
  const url = `${BASE_URL}/item/itemOrganListJung.json`;
  const body = { reportFormRootNo: primary, apbaType: [], jidtDptm: [], area: [], apba_id: "", pageNo: 1 };
  let organs: any[];
  try {
    const resp = await retryFetch(url, { method: "POST", jsonBody: body, headers: JSON_HEADERS, timeoutMs: 30000 });
    const node = (((await resp.json()) as any)?.data ?? {})?.organList ?? [];
    organs = Array.isArray(node) ? node : node?.result ?? [];
  } catch {
    return {};
  }
  const out: Record<string, any> = {};
  for (const o of organs ?? []) {
    if (want.has(o?.apbaId)) {
      out[o.apbaId] = {
        기관명: o?.apbaNa ?? "",
        disclosureNo: (o?.disclosureNo ?? "").trim(),
        submissionNo: (o?.submissionNo ?? "").trim(),
      };
    }
  }
  return out;
}

const DISCIPLINE_CATS = ["파면", "해임", "강등", "정직", "감봉", "견책"];
const YEAR_RE = /^(\d{4})년$/;

export function summarizeDisciplineTable(tables: string[][][]): any {
  const counts: Record<string, number> = {};
  for (const c of DISCIPLINE_CATS) counts[c] = 0;
  counts["기타"] = 0;
  const others: string[] = [];
  let total = 0;
  for (const tbl of tables ?? []) {
    if (!tbl?.length || !tbl[0].includes("징계종류")) continue;
    const col = tbl[0].indexOf("징계종류");
    for (const row of tbl.slice(1)) {
      if (col >= row.length) continue;
      const jong = (row[col] ?? "").trim();
      if (!jong) continue;
      total++;
      if (jong.includes("출근정지")) {
        counts["정직"]++;
        continue;
      }
      const matched = DISCIPLINE_CATS.find((c) => jong.includes(c));
      if (matched) counts[matched]++;
      else {
        counts["기타"]++;
        others.push(jong);
      }
    }
    break;
  }
  return { 징계건수: counts, 총건수: total, 기타종류: others };
}

export function summarizeIntegrityTable(tables: string[][][]): any {
  for (const tbl of tables ?? []) {
    if (!tbl?.length) continue;
    const yearCols: [number, string][] = [];
    tbl[0].forEach((h, i) => {
      const m = YEAR_RE.exec((h ?? "").trim());
      if (m) yearCols.push([i, m[1]]);
    });
    if (!yearCols.length) continue;
    const gradeRow = tbl.slice(1).find((r) => r?.length && (r[0] ?? "").includes("청렴도"));
    if (!gradeRow) continue;
    const grades: Record<string, string> = {};
    for (const [i, yr] of yearCols) {
      const val = i < gradeRow.length ? (gradeRow[i] ?? "").trim() : "";
      grades[yr] = val && val !== "해당없음" ? val : "-";
    }
    return { 연도별등급: grades, 연도: yearCols.map(([, yr]) => yr) };
  }
  return { 연도별등급: {}, 연도: [] };
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
