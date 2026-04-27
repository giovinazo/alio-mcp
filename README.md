# alio-mcp

한국 공공기관 정보공개시스템 **알리오(ALIO, [www.alio.go.kr](https://www.alio.go.kr))** 의 항목별공시 데이터를 LLM 도구로 노출하는 MCP(Model Context Protocol) 서버.

GUI 크롤러는 *사람이* 알리오를 쓰게 해주고, 이 MCP 서버는 *AI 에이전트가* 알리오를 쓰게 해준다.

## 무엇을 하는가

알리오 항목별공시는 모든 공공기관(약 344개)이 의무적으로 공시하는 83개 표준화된 정보 메뉴다(임직원 수·임원연봉·신규채용 현황·이사회·자체 감사부서 등). 이 MCP는 그 메뉴 체계를 LLM에 노출하여, 자연어로 "산단공이랑 정원 비슷한 기관 5곳 비교해줘" 같은 질의를 가능하게 한다.

## 제공 도구

### `list_menus(category="")`

알리오 항목별공시 메뉴 목록을 반환한다.

**인자**
- `category` *(string, optional)* — 대분류명
  - 허용값: `"기관운영"`, `"ESG 운영"`, `"경영성과"`, `"대내외 평가 등"`
  - 공백·대소문자 차이 자동 흡수 (`"esg운영"`도 매칭)
  - 빈 문자열이면 전체 83개 반환

**반환**
```json
[
  {"대분류": "기관운영", "항목명": "임직원 수", "rootNo": "20201,20202,20203,20204", "보고서형": true},
  {"대분류": "기관운영", "항목명": "자체 감사부서 현황", "rootNo": "32311", "보고서형": true}
]
```

매칭이 없으면 NOT_FOUND 시그널과 유효 대분류 목록 반환.

## 설치

```bash
git clone https://github.com/giovinazo/alio-mcp.git
cd alio-mcp
pip install -r requirements.txt
```

요구사항: Python 3.10+, `mcp>=1.0.0`, `requests>=2.31.0`

## 설정

### Claude Desktop / Claude Code

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) 또는 `~/.claude/settings.local.json`(Claude Code)에 다음 추가:

```json
{
  "mcpServers": {
    "alio": {
      "type": "stdio",
      "command": "python3",
      "args": ["/absolute/path/to/alio-mcp/alio_mcp.py"]
    }
  }
}
```

전체 예시는 [`examples/`](./examples) 폴더 참조.

## 사용 예시

Claude에서 자연어로:

> "알리오에서 기관운영 대분류에 어떤 메뉴들이 있어?"

→ 28개 항목 자동 반환

> "ESG 운영 분야 메뉴 보여줘"

→ 23개 항목 자동 반환 (공백 표기 차이는 흡수)

## 데이터 출처 / API 참고

알리오 사이트의 비공식 내부 API를 사용한다.

- `POST https://www.alio.go.kr/item/formList.json` — 메뉴 일괄 조회 (1회 호출 / 83개 일괄)

이 외에 기관 목록(`itemOrganListJung.json`)·다운로드 엔드포인트(`pdf.json`/`file.json`/`dfile.json`/`rulefiledown.json`) 4종이 알려져 있으며, 이후 버전에서 도구로 추가될 예정.

## 라이선스

[MIT](./LICENSE)

알리오에 공시되는 데이터 자체는 **공공누리 또는 공공데이터법**에 따른 공공기관 데이터로, 본 도구는 단순한 접근 인터페이스를 제공한다.

## 만든 이유

자체감사 업무 중 타 공공기관 벤치마킹·정원 비교·감사부서 현황 비교가 빈번한데, 매번 알리오 사이트에서 항목·기관·분기를 일일이 찾아 PDF를 다운로드해 표로 정리하는 작업이 비효율적이었다. AI 에이전트가 직접 알리오 데이터를 다룰 수 있다면 자연어 한 줄로 끝나리라는 가설을 검증하기 위해 만들었다.

## 변경 이력

- **v0.1.0** (2026-04-27) — 초기 공개. `list_menus` 단일 도구.

## 후속 계획

- [ ] `list_organs(rootNo, page)` — 항목별 공시 기관 목록
- [ ] `download_report(disclosureNo, kind)` — 보고서 PDF/첨부 다운로드 (4종 엔드포인트 자동 판별)
- [ ] `list_board_items(rootNo)` — 게시판형 12개 항목 처리

---

**English summary**: MCP server exposing the 83-menu structure of Korea's public institution disclosure system (ALIO). Lets LLM agents query, compare, and summarize disclosure data via natural language. PoC stage; one tool (`list_menus`).
