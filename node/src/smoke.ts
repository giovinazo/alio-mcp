/**
 * 스모크 테스트 — MCP 없이 core.ts 함수를 직접 호출해 알리오 API 연동을 검증.
 * 실행: npm run smoke
 */
import { loadPublicInstitutions } from "./core";

async function main(): Promise<void> {
  console.error("[smoke] loadPublicInstitutions() 호출 중...");
  const t0 = Date.now();
  const map = await loadPublicInstitutions();
  const ms = Date.now() - t0;
  console.error(`[smoke] 총 기관 수: ${map.size} (${ms}ms)`);

  if (map.size === 0) {
    console.error("[smoke] FAIL — 기관 0건. 네트워크/엔드포인트 확인 필요.");
    process.exit(1);
  }

  const keyword = "산업단지";
  const hits = [...map.keys()].filter((n) => n.includes(keyword));
  console.error(`[smoke] '${keyword}' 매칭 ${hits.length}건:`);
  for (const name of hits) {
    const v = map.get(name)!;
    console.error(`         - ${name} | ${v.apba_id} | ${v.inst_type} | ${v.dept} | ${v.region}`);
  }

  console.error("[smoke] PASS");
}

main().catch((err) => {
  console.error("[smoke] 예외:", err);
  process.exit(1);
});
