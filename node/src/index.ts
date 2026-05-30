/**
 * 알리오 항목별공시 MCP 서버 (alio_mcp.py의 TypeScript 포팅)
 *
 * 한국 공공기관 정보공개시스템 알리오(www.alio.go.kr)의 항목별공시
 * 데이터를 LLM 도구 12개로 노출한다. MCPB 번들로 패키징되어 Claude
 * Desktop에서 더블클릭 한 번으로 설치된다.
 *
 * 주의: stdout은 JSON-RPC 채널 — 모든 로깅은 console.error(stderr)로만.
 */
import * as path from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import {
  BASE_URL,
  JSON_HEADERS,
  RULE_DIVIS_CODES,
  retryFetch,
  fetchAlioItems,
  loadPublicInstitutions,
  fetchReportTables,
  fetchBoardAttachmentList,
  fetchBoardExternalLinks,
  downloadBoardAttachment,
  downloadAttachment,
  fetchAllBoardItems,
  fetchRuleList,
  fetchAllRules,
  fetchRuleDetail,
  downloadRuleFileToPath,
  sanitizeFilename,
  toBoardItem,
  ensureDir,
  fileSize,
  type Institution,
} from "./core";

const server = new McpServer({ name: "alio", version: "1.1.0" });
const DEFAULT_SAVE_DIR = "/tmp/alio_downloads";

/** dict/list 결과를 MCP text content로 직렬화 (한글 유니코드 보존). */
function jsonResult(obj: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(obj, null, 2) }] };
}

const normalize = (s: string) => s.replace(/ /g, "").toLowerCase();

// ─────────────────────────────────────────────────────────────
// Tool 1: list_menus — 메뉴 조회
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "list_menus",
  {
    title: "항목별공시 메뉴 목록",
    description:
      "알리오 항목별공시 메뉴 목록(92개)을 반환한다. 자주 쓰는 rootNo: 일반현황 10105, " +
      "임직원 수 20201, 징계현황 21201, 자체감사결과 43006, 감사원 지적사항 B1220, " +
      "경영 평가결과 B1230, 내부규정 21110 등. category(대분류)와 keyword(항목명 부분일치)는 " +
      "AND 조건으로 동시 사용 가능.",
    inputSchema: {
      category: z
        .string()
        .default("")
        .describe("대분류명(예: '기관운영','경영성과'). 공백·대소문자 무시. 빈 값이면 필터 없음."),
      keyword: z.string().default("").describe("항목명 부분 일치(예: '감사','징계'). 빈 값이면 필터 없음."),
    },
  },
  async ({ category, keyword }) => {
    let items = await fetchAlioItems();
    if (items.length === 0) return jsonResult([{ error: "REQUEST_FAILED: 알리오 API 응답 없음" }]);

    if (category) {
      const target = normalize(category);
      const filtered = items.filter((m) => normalize(m?.lcdnm ?? "") === target);
      if (filtered.length === 0) {
        const valid = [...new Set(items.map((m) => m?.lcdnm).filter(Boolean))].sort();
        return jsonResult([{ error: `NOT_FOUND: '${category}' 대분류 없음`, 유효한_대분류: valid }]);
      }
      items = filtered;
    }

    if (keyword) {
      const kw = keyword.trim();
      items = items.filter((m) => (m?.mcdnm ?? "").includes(kw));
      if (items.length === 0)
        return jsonResult([{ error: `NOT_FOUND: 항목명에 '${keyword}' 포함 메뉴 없음` }]);
    }

    return jsonResult(
      items.map((m) => ({
        대분류: m?.lcdnm ?? null,
        항목명: m?.mcdnm ?? null,
        rootNo: m?.reportNos || m?.mcd || null,
        보고서형: (m?.reportYn ?? "").toUpperCase() === "Y",
      }))
    );
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 2: list_organs — 항목별 공시 기관 목록
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "list_organs",
  {
    title: "항목별 공시 기관 목록",
    description:
      "특정 메뉴(rootNo)에 공시하는 기관 목록(약 355개). 콤마 다중 rootNo는 첫 항목만 사용. " +
      "반환: {totalCnt, page, 기관:[{기관ID, 기관명, 기관유형, 주무부처, 기준연도, 기준분기, 공시번호, 제출번호}]}.",
    inputSchema: {
      rootNo: z.string().describe("메뉴 rootNo (예: '10105' 일반현황, 'B1010' 임원 모집공고)."),
      page: z.coerce.number().int().default(1).describe("페이지 번호 (1부터)."),
    },
  },
  async ({ rootNo, page }) => {
    if (!rootNo) return jsonResult({ error: "MISSING: rootNo가 필수입니다" });
    const primary = rootNo.split(",")[0].trim();

    let body: any;
    try {
      const resp = await retryFetch(`${BASE_URL}/item/itemOrganListJung.json`, {
        method: "POST",
        jsonBody: {
          reportFormRootNo: primary,
          apbaType: [],
          jidtDptm: [],
          area: [],
          apba_id: "",
          pageNo: page,
        },
        headers: JSON_HEADERS,
        timeoutMs: 15000,
      });
      body = await resp.json();
    } catch (e: any) {
      return jsonResult({ error: `REQUEST_FAILED: ${e?.message ?? e}` });
    }

    if (body?.status && body.status !== "success") {
      return jsonResult({
        error: "ALIO_API_FAIL",
        rootNo: primary,
        message: body?.message ?? "알 수 없음",
        hint: "일부 rootNo는 알리오 자체 결함(예: 63601). 다른 rootNo 시도 또는 재시도.",
      });
    }

    const d = body?.data ?? {};
    const organs = d?.organList ?? [];
    if (!organs.length) return jsonResult({ error: `NOT_FOUND: rootNo='${primary}' 기관 목록 없음` });

    return jsonResult({
      totalCnt: d?.totalCnt ?? null,
      page,
      기관: organs.map((o: any) => ({
        기관ID: o?.apbaId,
        기관명: o?.apbaNa,
        기관유형: o?.typeNa,
        주무부처: o?.jidtNa,
        기준연도: o?.critYyyy,
        기준분기: o?.quartNa,
        공시번호: o?.disclosureNo,
        제출번호: o?.submissionNo,
      })),
    });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 3: list_board_items — 게시판형 자료 목록 (1페이지)
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "list_board_items",
  {
    title: "게시판형 자료 목록(1페이지)",
    description:
      "게시판형·보고서형 항목의 자료 목록(1페이지 최대 10건). 게시판형 예: B1010 임원 모집공고, " +
      "B1220 감사원 지적사항. 보고서형 예: 43006 자체감사결과. 43006·32301 등은 apbaId 필요할 수 있음. " +
      "응답 필드를 그대로 list_board_attachments 호출에 활용 가능.",
    inputSchema: {
      rootNo: z.string().describe("항목 rootNo (예: 'B1010', '43006')."),
      apbaId: z.string().default("").describe("특정 기관 ID로 한정(예: 'C0208'). 'B' 게시판형은 빈 값 가능."),
      page: z.coerce.number().int().default(1).describe("페이지 번호 (1부터)."),
    },
  },
  async ({ rootNo, apbaId, page }) => {
    if (!rootNo) return jsonResult({ error: "MISSING: rootNo가 필수입니다" });

    let body: any;
    try {
      const resp = await retryFetch(`${BASE_URL}/item/itemReportListSusi.json`, {
        method: "POST",
        jsonBody: {
          pageNo: page,
          apbaId,
          apbaType: "",
          reportFormRootNo: rootNo,
          search_word: "",
          search_flag: "title",
          bid_type: "",
          enfc_istt: "",
        },
        headers: JSON_HEADERS,
        timeoutMs: 15000,
      });
      body = await resp.json();
    } catch (e: any) {
      return jsonResult({ error: `REQUEST_FAILED: ${e?.message ?? e}` });
    }

    if (body?.status && body.status !== "success") {
      return jsonResult({ error: "ALIO_API_FAIL", rootNo, apbaId, message: body?.message ?? "알 수 없음" });
    }

    const d = body?.data ?? {};
    const items = d?.result ?? [];
    if (!items.length)
      return jsonResult({ error: `NOT_FOUND: rootNo='${rootNo}' apbaId='${apbaId}' 자료 없음` });

    return jsonResult({
      rootNo,
      page,
      자료: items.map(toBoardItem),
    });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 4: download_report — 공시 보고서 PDF
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "download_report",
  {
    title: "공시 보고서 PDF 다운로드",
    description:
      "공시번호(disclosureNo)로 보고서 PDF를 다운로드한다. 보고서형 메뉴(임직원수·일반현황 등)의 " +
      "공시번호로 호출. 반환: {saved_path, size_bytes}.",
    inputSchema: {
      disclosureNo: z.string().describe("공시번호 (list_organs/list_board_items 응답의 '공시번호')."),
      save_dir: z.string().default(DEFAULT_SAVE_DIR).describe("저장 디렉토리 (없으면 자동 생성)."),
      filename: z.string().default("").describe("저장 파일명. 빈 값이면 'alio_{disclosureNo}.pdf'."),
    },
  },
  async ({ disclosureNo, save_dir, filename }) => {
    if (!disclosureNo) return jsonResult({ error: "MISSING: disclosureNo가 필수입니다" });
    await ensureDir(save_dir);
    const fn = filename || `alio_${disclosureNo}.pdf`;
    const r = await downloadAttachment("pdf", { name: fn }, save_dir, disclosureNo);
    if (!r.success) return jsonResult({ error: `DOWNLOAD_FAILED: ${r.message}`, disclosureNo });
    return jsonResult({ saved_path: r.savedPath, size_bytes: await fileSize(r.savedPath) });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 12: get_report_data — 보고서 본문(표·평문) 조회
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "get_report_data",
  {
    title: "보고서 본문(표·평문) 조회",
    description:
      "보고서형 공시의 본문을 표·평문으로 반환(PDF/HWP 우회). download_report는 PDF를 저장만 하고 " +
      "본문을 돌려주지 않지만, 이 도구는 itemReportRight.do의 HTML 표를 파싱해 LLM이 바로 읽을 수 있는 " +
      "행렬·평문으로 반환한다. 징계현황·임직원수·복리후생비 등 보고서형 항목 내용을 파일 없이 확인. " +
      "공시번호는 list_organs/list_board_items 응답의 '공시번호'. 반환: {disclosureNo, 제목, 표_개수, 표, 본문텍스트}.",
    inputSchema: {
      disclosureNo: z
        .string()
        .describe("공시번호 (list_organs/list_board_items 응답의 '공시번호')."),
    },
  },
  async ({ disclosureNo }) => {
    if (!disclosureNo) return jsonResult({ error: "MISSING: disclosureNo가 필수입니다" });
    return jsonResult(await fetchReportTables(disclosureNo));
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 5: search_organs — 기관 검색 (이름·지역·유형)
// ─────────────────────────────────────────────────────────────
let instCache: Map<string, Institution> | null = null;

server.registerTool(
  "search_organs",
  {
    title: "기관 검색 (이름·지역·유형)",
    description:
      "공공기관 약 355개를 기관명·지역·기관유형으로 검색(부분 일치, AND). 첫 호출 시 기관목록 " +
      "API 1회 호출 후 캐시. 세 인자는 모두 부분 문자열이며 함께 주면 AND로 좁혀진다. 셋 다 비우면 " +
      "에러. 예: {region:'대구', org_type:'위탁집행'} → 대구 소재 위탁집행형 준정부기관 일괄. " +
      "반환: {총_검색결과, 조건, 기관:[{기관ID, 기관명, 기관유형, 주무부처, 지역}]} (상위 50건).",
    inputSchema: {
      name: z.string().default("").describe("기관명 부분 문자열. 예: '산업단지', '한국전력'."),
      region: z.string().default("").describe("소재지(본사) 부분 문자열. 예: '대구', '대구광역시', '세종'."),
      org_type: z
        .string()
        .default("")
        .describe("기관유형 부분 문자열. 예: '위탁집행', '준정부기관(위탁집행형)', '공기업', '기금관리', '기타공공기관'."),
    },
  },
  async ({ name, region, org_type }) => {
    const nm = (name ?? "").trim();
    const rg = (region ?? "").trim();
    const tp = (org_type ?? "").trim();
    if (!nm && !rg && !tp)
      return jsonResult({ error: "MISSING: name·region·org_type 중 최소 하나가 필요합니다" });
    if (!instCache) {
      instCache = await loadPublicInstitutions();
      if (instCache.size === 0) {
        instCache = null;
        return jsonResult({ error: "기관 목록 로드 실패" });
      }
    }
    const matches: Array<Record<string, string>> = [];
    for (const [instName, v] of instCache) {
      if (
        (!nm || instName.includes(nm)) &&
        (!rg || (v.region ?? "").includes(rg)) &&
        (!tp || (v.inst_type ?? "").includes(tp))
      ) {
        matches.push({
          기관ID: v.apba_id,
          기관명: instName,
          기관유형: v.inst_type,
          주무부처: v.dept,
          지역: v.region,
        });
      }
    }
    const 조건 = { name: nm, region: rg, org_type: tp };
    if (matches.length === 0)
      return jsonResult({ error: "NOT_FOUND: 조건에 맞는 기관 없음", 총_검색결과: 0, 조건 });
    return jsonResult({ 총_검색결과: matches.length, 조건, 기관: matches.slice(0, 50) });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 6: list_board_attachments — 게시판형 자료 첨부 메타
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "list_board_attachments",
  {
    title: "게시판형 자료 첨부 메타",
    description:
      "게시판형 자료의 첨부파일·외부링크 메타를 추출한다. list_board_items 응답 필드를 그대로 전달. " +
      "첨부 kind: 'upload'(spath/sfile) 또는 'fileno'(file_no). " +
      "반환: {첨부:[{kind, name, spath, sfile, file_no}], 외부링크:[{url, text}]}.",
    inputSchema: {
      apbaId: z.string().describe("기관ID (필수, 예: 'C0208')."),
      reportFormNo: z.string().describe("게시판형 항목 식별자 (필수, 예: 'B1220')."),
      idx: z.string().default("").describe("list_board_items 응답의 'idx'."),
      disclosureNo: z.string().default("").describe("list_board_items 응답의 '공시번호'."),
      tableName: z.string().default("").describe("list_board_items 응답에서 그대로 전달."),
      idxName: z.string().default("").describe("list_board_items 응답에서 그대로 전달."),
      bidType: z.string().default("").describe("list_board_items 응답에서 그대로 전달."),
    },
  },
  async ({ apbaId, reportFormNo, idx, disclosureNo, tableName, idxName, bidType }) => {
    if (!apbaId || !reportFormNo) return jsonResult({ error: "MISSING: apbaId, reportFormNo 필수" });
    const meta = {
      report_form_no: reportFormNo,
      disclosure_no: disclosureNo,
      idx,
      table_name: tableName,
      idx_name: idxName,
      bid_type: bidType,
    };
    const attachments = await fetchBoardAttachmentList(apbaId, meta);
    const extLinks = await fetchBoardExternalLinks(apbaId, meta);

    if (attachments.length === 0 && extLinks.length === 0) {
      return jsonResult({ error: "NOT_FOUND: 첨부 또는 외부링크 없음", 첨부: [], 외부링크: [] });
    }
    return jsonResult({
      첨부: attachments.map((a) => ({
        kind: a.kind,
        name: a.dfile ?? "",
        spath: a.spath ?? "",
        sfile: a.sfile ?? "",
        file_no: a.file_no ?? "",
      })),
      외부링크: extLinks,
    });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 7: download_board_attachment — 게시판형 첨부 다운로드
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "download_board_attachment",
  {
    title: "게시판형 첨부 다운로드",
    description:
      "게시판형 첨부파일을 다운로드한다. list_board_attachments 응답의 '첨부' 항목 필드를 그대로 전달. " +
      "kind='upload'는 spath/sfile, kind='fileno'는 file_no 필요. 반환: {saved_path, size_bytes}.",
    inputSchema: {
      kind: z.enum(["upload", "fileno"]).describe("'upload'(spath/sfile) 또는 'fileno'(file_no)."),
      name: z.string().default("").describe("저장 파일명 (옵션, 미지정 시 자동 명명)."),
      spath: z.string().default("").describe("kind='upload'일 때 알리오 upload 경로."),
      sfile: z.string().default("").describe("kind='upload'일 때 알리오 파일명."),
      file_no: z.string().default("").describe("kind='fileno'일 때 알리오 fileNo."),
      save_dir: z.string().default(DEFAULT_SAVE_DIR).describe("저장 디렉토리 (없으면 자동 생성)."),
    },
  },
  async ({ kind, name, spath, sfile, file_no, save_dir }) => {
    if (kind === "upload" && (!spath || !sfile))
      return jsonResult({ error: "MISSING: kind='upload'는 spath, sfile 필수" });
    if (kind === "fileno" && !file_no)
      return jsonResult({ error: "MISSING: kind='fileno'는 file_no 필수" });

    await ensureDir(save_dir);
    const r = await downloadBoardAttachment({ kind, dfile: name, spath, sfile, file_no }, save_dir);
    if (!r.success) return jsonResult({ error: `DOWNLOAD_FAILED: ${r.message}` });
    return jsonResult({ saved_path: r.savedPath, size_bytes: await fileSize(r.savedPath) });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 8: list_all_board_items — 게시판형 자료 전체 페이지 순회
// ─────────────────────────────────────────────────────────────
const APBA_REQUIRED_ROOTS = new Set(["43006", "32301"]);

server.registerTool(
  "list_all_board_items",
  {
    title: "게시판형 자료 전체 순회",
    description:
      "itemReportListSusi 응답을 모든 페이지에 걸쳐 자동 순회한다. 자체감사(43006)·경영실적평가(B1230)·" +
      "감사원 지적사항(B1220) 등 누적 자료군에 적합. 43006·32301 등 숫자 rootNo는 apbaId 필수. " +
      "반환: {rootNo, totalCnt, 자료:[...]}.",
    inputSchema: {
      rootNo: z.string().describe("자료 식별자(예: '43006','B1230','B1220')."),
      apbaId: z.string().default("").describe("기관 ID. 'B' 게시판형은 빈 값 가능, 숫자 rootNo는 필수."),
    },
  },
  async ({ rootNo, apbaId }) => {
    if (!rootNo) return jsonResult({ error: "MISSING: rootNo가 필수입니다" });
    const primary = rootNo.split(",")[0].trim();

    if (!apbaId && APBA_REQUIRED_ROOTS.has(primary)) {
      return jsonResult({
        error: `APBA_REQUIRED: rootNo='${primary}'는 apbaId(기관ID) 없이 전체 조회 불가`,
        hint: "search_organs(name)으로 기관ID를 먼저 확인한 뒤 apbaId를 지정하세요.",
        totalCnt: 0,
        자료: [],
      });
    }

    const items = await fetchAllBoardItems(rootNo, apbaId);
    if (items.length === 0) {
      const result: any = { error: `NOT_FOUND: rootNo='${rootNo}' apbaId='${apbaId}' 자료 없음`, totalCnt: 0, 자료: [] };
      if (!apbaId && !primary.startsWith("B")) {
        result.hint = "이 rootNo는 apbaId 필수일 수 있음. search_organs로 기관ID 확인 후 재시도.";
      }
      return jsonResult(result);
    }
    return jsonResult({ rootNo, totalCnt: items.length, 자료: items });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 9: download_disclosure_attachment — 보고서형 부속 첨부 file/dfile
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "download_disclosure_attachment",
  {
    title: "보고서형 부속 첨부 다운로드",
    description:
      "보고서형 공시의 부속 첨부파일(엑셀·한글 등)을 다운로드한다. kind='file'(fileId+disclosureNo) 또는 " +
      "kind='dfile'(fileName+submissionNo, 사망자수 70401). 반환: {saved_path, size_bytes}.",
    inputSchema: {
      kind: z.enum(["file", "dfile"]).describe("'file'(fileId+disclosureNo) 또는 'dfile'(fileName+submissionNo)."),
      fileName: z.string().describe("저장 파일명 + 'dfile' 식별자. 알리오 응답 원본명."),
      disclosureNo: z.string().default("").describe("공시번호 (kind='file'일 때 필수)."),
      submissionNo: z.string().default("").describe("제출번호 (kind='dfile'일 때 필수)."),
      fileId: z.string().default("").describe("파일 ID (kind='file'일 때 필수)."),
      save_dir: z.string().default(DEFAULT_SAVE_DIR).describe("저장 디렉토리 (없으면 자동 생성)."),
    },
  },
  async ({ kind, fileName, disclosureNo, submissionNo, fileId, save_dir }) => {
    if (!fileName) return jsonResult({ error: "MISSING: fileName이 필수입니다" });
    if (kind === "file" && (!fileId || !disclosureNo))
      return jsonResult({ error: "MISSING: kind='file'은 fileId + disclosureNo 필수" });
    if (kind === "dfile" && !submissionNo)
      return jsonResult({ error: "MISSING: kind='dfile'은 submissionNo 필수" });

    await ensureDir(save_dir);
    const r = await downloadAttachment(kind, { id: fileId, name: fileName }, save_dir, disclosureNo, submissionNo);
    if (!r.success) return jsonResult({ error: `DOWNLOAD_FAILED: ${r.message}` });
    return jsonResult({ saved_path: r.savedPath, size_bytes: await fileSize(r.savedPath) });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 10: list_rules — 기관 내부규정 목록 + 최신 파일 메타
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "list_rules",
  {
    title: "기관 내부규정 목록",
    description:
      "기관 내부규정 목록 조회. 기본은 전체 페이지 순회 + 각 규정 findRuleDtl로 최신 파일 메타까지 반환. " +
      "성능 옵션: count_only=true(totalCnt만, HTTP 1회), include_files=false(파일 메타 생략). " +
      "divis: 'K1500'정관/'K1100'인사·복무·징계/'K1200'보수/'K1300'직제/'K1400'기타.",
    inputSchema: {
      instName: z.string().describe("기관명(apbaNa 검색). 예: '한국산업단지공단'."),
      divis: z.string().default("").describe("분류 코드(빈 값=전체). K1500/K1100/K1200/K1300/K1400."),
      count_only: z.boolean().default(false).describe("true면 totalCnt만 반환(HTTP 1회)."),
      include_files: z.boolean().default(true).describe("false면 findRuleDtl 생략(파일 메타 없음)."),
    },
  },
  async ({ instName, divis, count_only, include_files }) => {
    if (!instName) return jsonResult({ error: "MISSING: instName이 필수입니다" });
    if (divis && !Object.values(RULE_DIVIS_CODES).includes(divis)) {
      return jsonResult({
        error: "INVALID: divis는 RULE_DIVIS_CODES 값 중 하나여야 함",
        유효한_divis: RULE_DIVIS_CODES,
      });
    }

    const divisLabel =
      Object.entries(RULE_DIVIS_CODES).find(([, v]) => v === divis)?.[0] ?? "전체";

    if (count_only) {
      const first = await fetchRuleList(instName, divis, 1);
      if (first.error && first.result.length === 0) {
        return jsonResult({ error: `FETCH_FAILED: ${first.error}`, instName, totalCnt: 0, 분류명: divisLabel });
      }
      return jsonResult({ instName, totalCnt: first.totalCnt ?? 0, 분류명: divisLabel });
    }

    const rules = await fetchAllRules(instName, divis);
    if (rules.length === 0)
      return jsonResult({ error: `NOT_FOUND: '${instName}' 내부규정 없음`, totalCnt: 0, 규정: [] });

    if (!include_files) {
      return jsonResult({
        totalCnt: rules.length,
        분류명: divisLabel,
        규정: rules.map((r) => ({
          seq: r?.seq ?? "",
          title: r?.title ?? "",
          insdRuleDivis: r?.insdRuleDivis ?? "",
        })),
      });
    }

    const result = [];
    for (const r of rules) {
      const seq = r?.seq ?? "";
      const detail = seq ? await fetchRuleDetail(seq) : { files: [], latest: null };
      result.push({
        seq,
        title: r?.title ?? "",
        insdRuleDivis: r?.insdRuleDivis ?? "",
        files_count: detail.files.length,
        latest: detail.latest,
      });
    }
    return jsonResult({ totalCnt: result.length, 분류명: divisLabel, 규정: result });
  }
);

// ─────────────────────────────────────────────────────────────
// Tool 11: download_rule_file — 내부규정 파일 다운로드
// ─────────────────────────────────────────────────────────────
server.registerTool(
  "download_rule_file",
  {
    title: "내부규정 파일 다운로드",
    description:
      "내부규정 파일을 fileNo로 단건 다운로드한다. list_rules 응답의 'latest.file_no'/'latest.file_name'을 " +
      "그대로 전달. 반환: {saved_path, size_bytes}.",
    inputSchema: {
      fileNo: z.string().describe("알리오 fileNo (list_rules → latest.file_no)."),
      fileName: z.string().default("").describe("저장 파일명. 빈 값이면 'rule_{fileNo}.bin'."),
      save_dir: z.string().default(DEFAULT_SAVE_DIR).describe("저장 디렉토리 (없으면 자동 생성)."),
    },
  },
  async ({ fileNo, fileName, save_dir }) => {
    if (!fileNo) return jsonResult({ error: "MISSING: fileNo가 필수입니다" });
    await ensureDir(save_dir);
    const safeName = sanitizeFilename(fileName || `rule_${fileNo}.bin`, 120);
    const savePath = path.join(save_dir, safeName);
    const r = await downloadRuleFileToPath(fileNo, savePath);
    if (!r.success) return jsonResult({ error: `DOWNLOAD_FAILED: ${r.message}`, fileNo });
    return jsonResult({ saved_path: r.savedPath, size_bytes: await fileSize(r.savedPath) });
  }
);

// ─────────────────────────────────────────────────────────────
// 서버 실행 (stdio)
// ─────────────────────────────────────────────────────────────
const transport = new StdioServerTransport();
await server.connect(transport);
console.error("[alio-mcp] stdio 서버 시작 (도구 11개)");
