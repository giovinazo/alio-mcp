# alio-mcp (Node / TypeScript)

알리오 MCP 서버의 Node/TypeScript 포팅. **MCPB 번들**(`.mcpb`)로 패키징되어 Claude Desktop에서
더블클릭 한 번으로 설치된다. Python 원본(`../alio_core.py`, `../alio_mcp.py`)과 도구 11개의
입출력·동작이 1:1 대응한다 (동등성 검증 완료).

## 빌드

```bash
npm install
npm run typecheck     # tsc --noEmit (타입 검사)
npm run build         # esbuild → server/index.js (단일 번들, node_modules 불필요)
node scripts/test_client.mjs   # 통합 테스트 (네트워크: 도구 11개 + 다운로드 바이트 검증)
```

## `.mcpb` 패키징

```bash
npx @anthropic-ai/mcpb validate manifest.json
npx @anthropic-ai/mcpb pack . alio-mcp.mcpb
```

생성된 `alio-mcp.mcpb`는 `manifest.json` + `server/index.js` + `icon.png` 3개 파일만 포함하며
(약 150KB), `.mcpbignore`로 `node_modules`·소스·테스트가 제외된다.

## 구조

| 파일 | 역할 | Python 대응 |
|------|------|-------------|
| `src/core.ts` | 알리오 API 호출·재시도·다운로드·HTML 파싱 | `alio_core.py` |
| `src/index.ts` | MCP 도구 11개 (FastMCP → `@modelcontextprotocol/sdk`) | `alio_mcp.py` |
| `manifest.json` | MCPB 매니페스트 (`server.type: node`) | — |
| `scripts/test_client.mjs` | MCP 클라이언트 통합 테스트 | — |

## 런타임

- HTTP: Node 내장 `fetch` + `AbortController` 타임아웃 (외부 HTTP 의존성 없음)
- 알리오 응답은 UTF-8 — 별도 인코딩 처리 불필요
- stdout은 JSON-RPC 채널이므로 모든 로깅은 `console.error`(stderr)로만
