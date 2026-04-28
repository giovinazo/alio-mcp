# alio-mcp

한국 공공기관 정보공개시스템 **알리오(ALIO, [www.alio.go.kr](https://www.alio.go.kr))** 의 항목별공시 데이터를 LLM 도구로 노출하는 MCP(Model Context Protocol) 서버.

GUI 크롤러는 *사람이* 알리오를 쓰게 해주고, 이 MCP 서버는 *AI 에이전트가* 알리오를 쓰게 해준다.

## 무엇을 하는가

알리오 항목별공시는 약 344개 공공기관이 의무적으로 공시하는 83개 표준화된 정보 메뉴다(임직원 수·임원연봉·신규채용 현황·이사회·자체 감사부서·임원 모집공고 등). 이 MCP는 그 데이터를 LLM이 자연어로 직접 다룰 수 있게 한다.

> *예* — "산단공이랑 정원 비슷한 기관 5곳 임직원수 비교해줘", "최근 30일 임원 모집공고를 부처별로 정리해줘", "산단공 비상임감사 모집공고 PDF 받아줘"

## 제공 도구 (v0.2.0 — 4개)

### 1. `list_menus(category="")`

알리오 항목별공시 메뉴 83개 목록 조회.

**인자**
- `category` *(string, optional)* — 대분류명. 허용값: `"기관운영"` / `"ESG 운영"` / `"경영성과"` / `"대내외 평가 등"`
  - 공백·대소문자 차이 자동 흡수 (`"esg운영"`도 매칭)
  - 빈 문자열이면 전체 83개 반환

**반환 예**
```json
[
  {"대분류": "기관운영", "항목명": "임직원 수", "rootNo": "20201,20202,20203,20204", "보고서형": true},
  {"대분류": "기관운영", "항목명": "임원 모집공고", "rootNo": "B1010", "보고서형": false}
]
```

### 2. `list_organs(rootNo, page=1)`

특정 메뉴(rootNo)에 공시하는 약 344개 기관 목록 조회.

**인자**
- `rootNo` *(string, required)* — 메뉴 식별자 (예: `"10105"` 일반현황, `"B1010"` 임원 모집공고)
  - 콤마 묶음(`"20201,20202,20203,20204"`)은 자동으로 첫 항목만 사용
- `page` *(int, optional, 기본 1)* — 페이지 번호

**반환 예**
```json
{
  "totalCnt": 344,
  "page": 1,
  "기관": [
    {
      "기관ID": "C0208", "기관명": "한국산업단지공단",
      "기관유형": "준정부기관(위탁집행형)", "주무부처": "산업통상부",
      "기준연도": "2025", "기준분기": null,
      "공시번호": null, "제출번호": null
    }
  ]
}
```

### 3. `list_board_items(rootNo, apbaId="", page=1)`

게시판형 12종 항목(B1010 임원 모집공고·B1020 직원 채용·B1030 입찰공고·B1210 국회 외부평가·B1220 감사원 지적사항 등)의 자료 목록 조회.

**인자**
- `rootNo` *(string, required)* — 게시판형 항목 (예: `"B1010"`)
- `apbaId` *(string, optional)* — 특정 기관 ID로 한정 (`"C0208"` 한국산업단지공단). 빈 문자열이면 전체 기관
- `page` *(int, optional, 기본 1)*

**반환 예**
```json
{
  "rootNo": "B1010", "page": 1,
  "자료": [
    {
      "제목": "한국산업단지공단 비상임감사 모집공고",
      "등록일": "2026.03.19",
      "기관ID": "C0208",
      "공시번호": "2026031903132783",
      "제출번호": "2026031910347384",
      "idx": "3502813",
      "reportFormNo": "B1010"
    }
  ]
}
```

### 4. `download_report(disclosureNo, save_dir="/tmp/alio_downloads", filename="")`

공시번호로 보고서 PDF 다운로드.

**인자**
- `disclosureNo` *(string, required)* — `list_organs` 또는 `list_board_items` 응답의 *공시번호*
- `save_dir` *(string, optional)* — 저장 디렉토리 (없으면 자동 생성)
- `filename` *(string, optional)* — 저장 파일명. 빈 문자열이면 `alio_{disclosureNo}.pdf`

**반환 예**
```json
{
  "saved_path": "/tmp/alio_downloads/alio_2025101403058502.pdf",
  "size_bytes": 90291,
  "content_type": "application/pdf"
}
```

매칭 실패 또는 인자 누락 시 모두 `{"error": "..."}` 시그널 반환 (NOT_FOUND·MISSING·HTTP·API_ERROR).

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

Claude에서 자연어 한 줄로:

| 질의 | 동원되는 도구 |
|---|---|
| "기관운영 대분류 메뉴 보여줘" | `list_menus("기관운영")` |
| "한국산업단지공단의 일반현황 공시번호 찾아줘" | `list_organs("10105")` → apbaId 필터링 |
| "산단공 최근 임원 모집공고 가져와" | `list_board_items("B1010", "C0208")` |
| "그 공고 PDF 받아줘" | `download_report(disclosureNo, save_dir)` |

## 데이터 출처 / API 참고

알리오 사이트의 비공식 내부 API를 사용한다.

| 엔드포인트 | 용도 | 사용 도구 |
|---|---|---|
| `POST /item/formList.json` | 83개 메뉴 일괄 조회 | `list_menus` |
| `POST /item/itemOrganListJung.json` | 메뉴별 기관 목록 (344개) | `list_organs` |
| `POST /item/itemReportListSusi.json` | 게시판형 자료 목록 | `list_board_items` |
| `GET /download/pdf.json` | 보고서 PDF 다운로드 | `download_report` |

기타 다운로드 엔드포인트(`file.json`·`dfile.json`·`rulefiledown.json`) 및 게시판형 첨부파일 직접 다운로드는 후속 버전에서 도구로 추가 예정.

## 라이선스

[MIT](./LICENSE)

알리오에 공시되는 데이터 자체는 **공공누리 또는 공공데이터법**에 따른 공공기관 데이터로, 본 도구는 단순한 접근 인터페이스를 제공한다.

## 만든 이유

자체감사 업무 중 타 공공기관 벤치마킹·정원 비교·감사부서 현황 비교가 빈번한데, 매번 알리오 사이트에서 항목·기관·분기를 일일이 찾아 PDF를 다운로드해 표로 정리하는 작업이 비효율적이었다. AI 에이전트가 직접 알리오 데이터를 다룰 수 있다면 자연어 한 줄로 끝나리라는 가설을 검증하기 위해 만들었다.

## 변경 이력

- **v0.2.0** (2026-04-28) — 도구 3종 추가 (`list_organs`, `list_board_items`, `download_report`)
- **v0.1.0** (2026-04-27) — 초기 공개. `list_menus` 단일 도구.

## 후속 계획

- [ ] `download_attachment` — 일반 첨부(`file.json`)·안전경영책임보고서(`dfile.json`)·내부규정(`rulefiledown.json`) 엔드포인트 통합
- [ ] `download_board_attachment` — 게시판형 첨부파일 직접 다운로드 (`itemBoard{rfn}.do` HTML 파싱)
- [ ] `search_organs` — 기관명 부분 일치 검색 도구

---

**English summary**: MCP server exposing the disclosure data of Korea's public institutions (ALIO). Provides 4 tools: menu listing, institution listing, board-type item listing, and report PDF download. v0.2.0.
