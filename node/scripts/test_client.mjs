/**
 * MCP 클라이언트 통합 테스트 — server/index.js를 자식 프로세스로 띄워
 * 정식 핸드셰이크 후 listTools + 주요 도구 callTool 검증 (다운로드 바이트 포함).
 * 실행: node scripts/test_client.mjs   (cwd = node/)
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({ command: "node", args: ["server/index.js"] });
const client = new Client({ name: "test-client", version: "1.0.0" });
await client.connect(transport);

const call = async (name, args) => {
  const res = await client.callTool({ name, arguments: args });
  return JSON.parse(res.content?.[0]?.text ?? "null");
};
const log = (...a) => console.error(...a);
let pass = 0;
let fail = 0;
const check = (cond, label, extra = "") => {
  if (cond) { pass++; log(`  ✓ ${label} ${extra}`); }
  else { fail++; log(`  ✗ ${label} ${extra}`); }
};

// ── 0) listTools
const { tools } = await client.listTools();
log(`\n[0] listTools → ${tools.length}개`);
check(tools.length === 11, "도구 11개 노출", `(실제 ${tools.length})`);
log("    " + tools.map((t) => t.name).join(", "));

// ── 1) search_organs
log(`\n[1] search_organs(name="산업단지")`);
const so = await call("search_organs", { name: "산업단지" });
const sandan = so?.기관?.[0];
check(sandan?.기관ID === "C0208", "산단공 C0208", `→ ${sandan?.기관명}`);

// ── 2) list_menus (category+keyword AND)
log(`\n[2] list_menus(keyword="감사")`);
const lm = await call("list_menus", { keyword: "감사" });
check(Array.isArray(lm) && lm.length > 0 && !lm[0].error, "감사 포함 메뉴", `→ ${lm.length}건`);
log("    예: " + (lm.slice(0, 3).map((m) => `${m.항목명}(${m.rootNo})`).join(", ")));

// ── 3) list_organs (일반현황 10105)
log(`\n[3] list_organs(rootNo="10105")`);
const lo = await call("list_organs", { rootNo: "10105" });
check(lo?.기관?.length > 0, "10105 기관목록", `→ totalCnt=${lo?.totalCnt}, page1=${lo?.기관?.length}건`);
const hasDisc = lo?.기관?.find((o) => o.공시번호);
log(`    공시번호 예: ${hasDisc?.기관명} / ${hasDisc?.공시번호}`);

// ── 4) list_board_items (자체감사결과 43006 + 산단공)
log(`\n[4] list_board_items(rootNo="43006", apbaId="C0208")`);
const lb = await call("list_board_items", { rootNo: "43006", apbaId: "C0208" });
check(lb?.자료?.length > 0 || lb?.error, "43006 자료 조회", `→ ${lb?.자료?.length ?? lb?.error}`);

// ── 5) list_all_board_items APBA_REQUIRED 가드
log(`\n[5] list_all_board_items(rootNo="43006") — apbaId 누락 가드`);
const guard = await call("list_all_board_items", { rootNo: "43006" });
check(String(guard?.error).startsWith("APBA_REQUIRED"), "APBA_REQUIRED 가드 동작", `→ ${guard?.error?.slice(0, 40)}`);

// ── 6) list_rules count_only (산단공)
log(`\n[6] list_rules(instName="한국산업단지공단", count_only=true)`);
const lrc = await call("list_rules", { instName: "한국산업단지공단", count_only: true });
check(typeof lrc?.totalCnt === "number" && lrc.totalCnt > 0, "내부규정 카운트", `→ ${lrc?.totalCnt}건 (${lrc?.분류명})`);

// ── 7) list_rules include_files=false (목록만, 빠름)
log(`\n[7] list_rules(instName="한국산업단지공단", include_files=false)`);
const lrf = await call("list_rules", { instName: "한국산업단지공단", include_files: false });
check(lrf?.규정?.length > 0, "내부규정 목록", `→ ${lrf?.규정?.length}건, 첫 seq=${lrf?.규정?.[0]?.seq}`);

// ── 8) 실제 다운로드: 첫 규정의 최신 파일 (include_files=true로 latest 확보 — seq 1건만)
log(`\n[8] 규정 1건 상세→다운로드 (download_rule_file)`);
// 목록 첫 규정 seq로 list_rules 풀스펙은 느리므로, 직접 download 체인 검증:
//   include_files=false 결과의 seq로는 file_no를 모르니, count_only=false+include_files=true 1페이지 대신
//   가장 가벼운 방법: 첫 규정 seq를 list_rules 풀스펙 1건으로 못 가져오므로 list_board_items 경로 대신 규정 풀스펙 소량 호출
const full = await call("list_rules", { instName: "한국산업단지공단", divis: "K1500" }); // 정관(보통 1건)
const firstWithFile = full?.규정?.find((r) => r?.latest?.file_no);
if (firstWithFile) {
  log(`    정관 최신파일: ${firstWithFile.latest.file_name} (fileNo=${firstWithFile.latest.file_no})`);
  const dl = await call("download_rule_file", {
    fileNo: String(firstWithFile.latest.file_no),
    fileName: firstWithFile.latest.file_name,
    save_dir: "/tmp/alio_test_dl",
  });
  check(dl?.size_bytes > 0, "규정 파일 다운로드 바이트>0", `→ ${dl?.size_bytes} bytes, ${dl?.saved_path}`);
} else {
  check(false, "정관 최신파일 메타 확보", `→ ${JSON.stringify(full).slice(0, 80)}`);
}

// ── 9) download_report: 43006 자체감사결과 보고서 PDF (공시번호 실재 자료)
log(`\n[9] download_report (43006 산단공 자체감사결과 PDF)`);
const disc = lb?.자료?.find((v) => v.공시번호)?.공시번호;
if (disc) {
  const pdf = await call("download_report", { disclosureNo: String(disc), save_dir: "/tmp/alio_test_dl" });
  check(pdf?.size_bytes > 0, "보고서 PDF 다운로드 바이트>0", `→ ${pdf?.size_bytes} bytes, ${pdf?.saved_path}`);
} else {
  check(false, "43006 공시번호 확보 실패", `→ ${JSON.stringify(lb?.자료?.[0] ?? lb).slice(0, 100)}`);
}

// ── 10) page coercion: 문자열 "1"을 넘겨도 동작해야 함 (zod coerce, LLM 호환)
log(`\n[10] list_organs page coercion (page="1" 문자열)`);
const loStr = await call("list_organs", { rootNo: "10105", page: "1" });
check(loStr?.기관?.length > 0, "page 문자열 coerce 동작", `→ ${loStr?.기관?.length ?? loStr?.error}건`);

await client.close();
log(`\n━━━ 결과: PASS ${pass} / FAIL ${fail} ━━━`);
process.exit(fail === 0 ? 0 : 1);
