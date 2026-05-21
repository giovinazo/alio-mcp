# alio-mcp

한국 공공기관 정보공개시스템 **알리오(ALIO, [www.alio.go.kr](https://www.alio.go.kr))** 의 항목별공시 데이터를 LLM 도구로 노출하는 MCP(Model Context Protocol) 서버.

GUI 크롤러는 *사람이* 알리오를 쓰게 해주고, 이 MCP 서버는 *AI 에이전트가* 알리오를 쓰게 해준다.

## 무엇을 하는가

알리오 항목별공시는 약 355개 공공기관이 의무적으로 공시하는 92개 표준화된 정보 메뉴다(임직원 수·임원연봉·신규채용 현황·이사회·자체 감사부서·임원 모집공고·감사원 지적사항 등). 이 MCP는 그 데이터를 LLM이 자연어로 직접 다룰 수 있게 한다.

> *예* — "산단공이랑 정원 비슷한 기관 5곳 임직원수 비교해줘", "최근 30일 임원 모집공고를 부처별로 정리해줘", "산단공 감사원 지적사항 첨부 PDF 다 받아줘"

## 아키텍처 (v0.4.0)

이 패키지의 `alio_core.py`는 알리오 API 호출·HTML 파싱·파일 다운로드를 담당하는 **공유 라이브러리(정본)**다. GUI 크롤러([alio-crawler](https://github.com/giovinazo/alio-crawler))는 자기 레포에 이 파일의 **sync된 사본**을 보유하며, 두 프로젝트가 동일 코어로 동작한다.

**동기화 절차** (alio_core.py 수정 후):
```bash
./sync_to_crawler.sh   # 형제 폴더 "1. 알리오 크롤러"로 cp
# 또는 다른 위치:
CRAWLER_DIR=/path/to/alio-crawler ./sync_to_crawler.sh
```

본 레포 단독으로 MCP 서버 실행에 알리오-크롤러는 필요 없다.

## 제공 도구 (v0.4.0 — 11개)

| # | 도구 | 도입 | 용도 |
|---|---|---|---|
| 1 | `list_menus` | v0.1.0 | 메뉴 92개 |
| 2 | `list_organs` | v0.2.0 | 메뉴별 기관 ~355개 |
| 3 | `list_board_items` | v0.2.0 | 게시판형 자료 1페이지 |
| 4 | `download_report` | v0.2.0 | 공시 PDF |
| 5 | `search_organs` | v0.3.0 | 기관명 부분 일치 |
| 6 | `list_board_attachments` | v0.3.0 | 게시판형 첨부 메타 |
| 7 | `download_board_attachment` | v0.3.0 | 게시판형 첨부 다운로드 |
| 8 | `list_all_board_items` | **v0.4.0** | itemReportListSusi 전체 페이지 자동 순회 (audit·mgmt_eval 통합) |
| 9 | `download_disclosure_attachment` | **v0.4.0** | 보고서 부속 첨부 file·dfile |
| 10 | `list_rules` | **v0.4.1** | 기관 내부규정 목록 + 최신 파일 메타 (v0.4.1 `count_only`·`include_files` 경량 옵션) |
| 11 | `download_rule_file` | **v0.4.0** | 내부규정 파일 fileNo 다운로드 |


### 1. `list_menus(category="")`

알리오 항목별공시 메뉴 92개 목록 조회 (v5.4.2 기준 — ESG 운영·AI 활용 카테고리 신설 반영).

**인자**
- `category` *(string, optional)* — 대분류명. 허용값: `"기관운영"` / `"ESG 운영"` / `"경영성과"` / `"대내외 평가 등"` / `"AI 활용"`
  - 공백·대소문자 차이 자동 흡수 (`"esg운영"`도 매칭)
  - 빈 문자열이면 전체 92개 반환

**반환 예**
```json
[
  {"대분류": "기관운영", "항목명": "임직원 수", "rootNo": "20201,20202,20203,20204", "보고서형": true},
  {"대분류": "기관운영", "항목명": "임원 모집공고", "rootNo": "B1010", "보고서형": false}
]
```

### 2. `list_organs(rootNo, page=1)`

특정 메뉴(rootNo)에 공시하는 약 355개 기관 목록 조회.

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

### 5. `search_organs(name)` *(v0.3.0 신규)*

공공기관 약 355개 중 기관명 부분 일치 검색.

**인자**
- `name` *(string, required)* — 검색 키워드 (부분 문자열). 예: `"산업단지"`, `"한국전력"`

**반환 예**
```json
{
  "총_검색결과": 1,
  "기관": [
    {"기관ID": "C0208", "기관명": "한국산업단지공단",
     "기관유형": "준정부기관(위탁집행형)", "주무부처": "산업통상부", "지역": "대구광역시"}
  ]
}
```

첫 호출 시 알리오 기관목록 API를 1회 호출해 캐시. 상위 50건 반환.

### 6. `list_board_attachments(apbaId, reportFormNo, idx, disclosureNo, ...)` *(v0.3.0 신규)*

게시판형 자료의 첨부파일·외부링크 메타 추출 (`itemBoard{reportFormNo}.do` HTML 파싱).

감사원 지적사항(B1220)·국회 외부평가(B1210)·임원 모집공고(B1010)·직원 채용(B1020) 등에서 첨부 PDF/HWP 메타를 얻는다.

**인자** (모두 `list_board_items` 응답에서 그대로 전달)
- `apbaId` *(string, required)* — 기관ID
- `reportFormNo` *(string, required)* — 게시판 항목 코드
- `idx`, `disclosureNo`, `tableName`, `idxName`, `bidType` *(string, optional)*

**반환 예**
```json
{
  "첨부": [
    {"kind": "upload", "name": "2025년도 산업통상자원부 종합감사 결과.pdf",
     "spath": "/2025/...", "sfile": "abc.pdf", "file_no": ""}
  ],
  "외부링크": [{"url": "https://www.g2b.go.kr/...", "text": "나라장터 입찰공고"}]
}
```

두 가지 첨부 패턴 통합:
- `kind="upload"`: `/upload{spath}{sfile}` 직접 GET (감사원/국회 지적사항)
- `kind="fileno"`: `/download/download.json?fileNo=N` GET (임원 모집공고·직원 채용)

### 7. `download_board_attachment(kind, name, spath, sfile, file_no, save_dir)` *(v0.3.0 신규)*

게시판형 첨부파일 다운로드. `list_board_attachments` 응답의 `첨부` 항목 필드를 그대로 전달.

**인자**
- `kind` *(string, required)* — `"upload"` 또는 `"fileno"`
- `name` *(string, optional)* — 저장 파일명
- `spath`, `sfile` *(string)* — `kind="upload"`일 때
- `file_no` *(string)* — `kind="fileno"`일 때
- `save_dir` *(string, optional, 기본 `/tmp/alio_downloads`)*

**반환 예**
```json
{
  "saved_path": "/tmp/alio_downloads/2025년도 산업통상자원부 종합감사 결과.pdf",
  "size_bytes": 75824
}
```

매칭 실패 또는 인자 누락 시 모두 `{"error": "..."}` 시그널 반환 (NOT_FOUND·MISSING·HTTP·API_ERROR·DOWNLOAD_FAILED).

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
| "산단공 기관ID 찾아줘" | `search_organs("산업단지공단")` |
| "한국산업단지공단의 일반현황 공시번호 찾아줘" | `list_organs("10105")` → apbaId 필터링 |
| "산단공 최근 감사원 지적사항 가져와" | `list_board_items("B1220", "C0208")` |
| "그 자료 첨부 PDF 다 받아줘" | `list_board_attachments(...)` → `download_board_attachment(...)` × N |
| "산단공 비상임감사 모집공고 PDF 받아줘" | `list_board_items("B1010", "C0208")` → `download_report(disclosureNo)` |
| "산단공 자체감사 결과 다 가져와" *(v0.4.0)* | `list_all_board_items("43006", "C0208")` — 79건 일괄 |
| "산단공 정관 최신 HWP 받아줘" *(v0.4.0)* | `list_rules("한국산업단지공단", "K1500")` → `download_rule_file(fileNo)` |
| "위탁집행형 준정부기관 49곳 규정 수 집계해줘" *(v0.4.1)* | 기관별 `list_rules(instName, count_only=True)` × 49 (HTTP 49회) |
| "산단공 정관 제목만 다 보여줘" *(v0.4.1)* | `list_rules("한국산업단지공단", "K1500", include_files=False)` |

## 데이터 출처 / API 참고

알리오 사이트의 비공식 내부 API를 사용한다.

| 엔드포인트 | 용도 | 사용 도구 |
|---|---|---|
| `POST /item/formList.json` | 83개 메뉴 일괄 조회 | `list_menus` |
| `POST /item/itemOrganListJung.json` | 메뉴별 기관 목록 (344개) | `list_organs` |
| `POST /item/itemReportListSusi.json` | 게시판형 자료 목록 | `list_board_items` |
| `GET /download/pdf.json` | 보고서 PDF 다운로드 | `download_report` |

| `GET /item/itemBoard{rfn}.do` (HTML 파싱) | 게시판형 첨부·외부링크 메타 추출 | `list_board_attachments` |
| `GET /upload{spath}{sfile}` | 게시판형 첨부 (패턴 A) | `download_board_attachment` |
| `GET /download/download.json?fileNo=N` | 게시판형 첨부 (패턴 B) | `download_board_attachment` |
| `POST /organ/findOrganApbaList.json` | 공공기관 전체 목록 (지역 포함) | `search_organs` |

보고서형 부속 첨부(`file.json`)·안전경영책임보고서(`dfile.json`)·내부규정(`rulefiledown.json`)은 후속 버전에서 도구로 추가 예정 (코어 함수 `download_attachment`는 이미 구현되어 있어 노출만 남음).

## 라이선스

[MIT](./LICENSE)

알리오에 공시되는 데이터 자체는 **공공누리 또는 공공데이터법**에 따른 공공기관 데이터로, 본 도구는 단순한 접근 인터페이스를 제공한다.

## 만든 이유

자체감사 업무 중 타 공공기관 벤치마킹·정원 비교·감사부서 현황 비교가 빈번한데, 매번 알리오 사이트에서 항목·기관·분기를 일일이 찾아 PDF를 다운로드해 표로 정리하는 작업이 비효율적이었다. AI 에이전트가 직접 알리오 데이터를 다룰 수 있다면 자연어 한 줄로 끝나리라는 가설을 검증하기 위해 만들었다.

## 변경 이력

- **v0.4.1** (2026-05-21) — `list_rules` 경량 옵션 2종 추가. `count_only=True`는 findRuleList 1페이지만 호출해 `totalCnt`만 반환(다수 기관 카운트 집계용, HTTP 1회). `include_files=False`는 findRuleDtl 호출을 생략(파일 메타 없이 제목·seq만, HTTP 호출은 페이지 수만큼). 기존 풀스펙 동작은 기본값 유지(하위호환). 한국국토정보공사 158건 기준 174회 → 1회(`count_only`) / 16회(`include_files=False`).
- **v0.4.0** (2026-05-19) — 도구 4종 추가: `list_all_board_items` (페이지 자동 순회로 audit·mgmt_eval·감사원 등 통합 처리), `download_disclosure_attachment` (보고서 부속 file·dfile), `list_rules` + `download_rule_file` (내부규정 체인 — findRuleList → findRuleDtl → rulefiledown). `self_check.py` 신설 (11개 도구 라이브 점검, PASS=12/13).
- **v0.3.0** (2026-05-19) — `alio_core.py` 도입(alio-crawler v5.4 다운로드 코어 공유). 도구 3종 추가 (`search_organs`, `list_board_attachments`, `download_board_attachment`). 메뉴 수 83→92개 (ESG 운영·AI 활용 카테고리 신설). 기관 수 344→355개.
- **v0.2.0** (2026-04-28) — 도구 3종 추가 (`list_organs`, `list_board_items`, `download_report`)
- **v0.1.0** (2026-04-27) — 초기 공개. `list_menus` 단일 도구.

## 후속 계획

- [x] ~~`download_disclosure_attachment` (file·dfile)~~ (v0.4.0 완료)
- [x] ~~`download_rule_file` 체인~~ (v0.4.0 완료)
- [x] ~~`download_board_attachment`, `search_organs`~~ (v0.3.0 완료)
- [ ] 입찰공고(B1030) 외부링크 일괄 추출 도구 (현재는 `list_board_attachments`의 `외부링크` 필드로 노출)
- [ ] alio 사이트 패턴 변동 자동 감지·알림 (cron + diff)

---

**English summary**: MCP server exposing the disclosure data of Korea's public institutions (ALIO). Provides 4 tools: menu listing, institution listing, board-type item listing, and report PDF download. v0.2.0.
