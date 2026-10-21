---
name: alio-investigator
description: 알리오(공공기관 경영정보 공개시스템, alio.go.kr) 공시 데이터를 조회·다운로드·기관별 비교하는 조사 지침. "알리오 확장(alio-mcp)" 도구가 설치되어 있을 때 쓴다. 사용자가 "알리오", "공공기관 공시", "○○공단 내부규정 찾아줘", "임원연봉 비교", "감사원 지적사항 받아줘", "징계현황", "청렴도 등급", "손익계산서·감사보고서 첨부 받아줘", "여러 기관 비교"처럼 공공기관 공시 자료 수집·비교를 요청할 때 사용한다. 법령 해석·위반 판단·보고서 작성은 이 스킬 범위가 아니다.
---

# 알리오 조사관

공공기관 감사·경영분석 업무를 돕는 알리오 공시 데이터 조사 지침입니다. 알리오 확장(alio-mcp)의 17개 도구를 상황에 맞게 조합해 자료를 찾고 받아 오며, 결과는 표·파일 목록으로 정리합니다.

## 도구 고르는 법

| 하려는 일 | 도구 순서 |
|---|---|
| 항목 번호(rootNo) 찾기 | `list_menus`(keyword="감사" 등) 또는 `list_menus_tree` |
| 기관 찾기 | `search_organs`(기관명·지역·기관유형) |
| 기관 개요 | `get_organ_profile` |
| 보고서 숫자를 바로 보기 | `list_organs`(rootNo) → 공시번호 → `get_report_data` |
| 보고서 PDF 받기 | `list_organs`(rootNo) → 공시번호 → `download_report` |
| 감사보고서·손익계산서 등 **부속 첨부** 받기 | `list_organs` → 공시번호 → `list_disclosure_attachments` → `download_disclosure_attachment` |
| 감사원·국회 지적사항 같은 **게시판형** 자료 | `list_all_board_items`(rootNo, apbaId) → `list_board_attachments` → `download_board_attachment` |
| **내부규정** 원문(.hwp·.hwpx) | `list_rules`(기관명, 분류) → `download_rule_file`(latest.file_no, latest.file_name) |
| 여러 기관 같은 항목 비교 | `compare_organs` |
| 징계 건수·청렴도 등급 집계 | `get_structured_summary` |

## 꼭 지킬 점

- **부속 첨부**는 `list_organs` 결과만으로는 파일 번호를 알 수 없습니다. 반드시 `list_disclosure_attachments`를 먼저 부릅니다. (kind='file'은 fileNo를 fileId로, kind='dfile'은 fileName+submissionNo)
- **내부규정**은 게시판형 도구로 보면 첨부가 비어 보입니다. `list_rules` + `download_rule_file`을 1순위로 씁니다. 규정이 많은 기관은 목록 조회에 1분 이상 걸릴 수 있습니다.
- 숫자로 된 게시판형 rootNo(예: 43006 내부·외부 감사결과, 32301 외부회계감사보고서)는 기관 코드(apbaId)가 필요합니다. 먼저 `search_organs`로 코드를 얻습니다.
- 파일은 확장 설치 때 정한 다운로드 폴더(기본 `Downloads/alio`)에 저장됩니다. 받은 파일의 경로를 결과에 꼭 적습니다.

## 결과 정리 방식

1. 표·목록 위주로 짧게. 풀어 쓴 설명은 줄입니다.
2. 대상 기관 수·성공 건수·실패 건수를 대조해 누락을 밝힙니다.
3. 실패는 기관명과 사유(404, 공시 없음 등)를 따로 적습니다.

예시:

```
## 수집 결과
- 항목: 임원연봉 (rootNo=20501)
- 대상 기관 15개 / 성공 14건 / 실패 1건

| 기관 | 파일 | 저장 위치 |
|---|---|---|
| 한국산업단지공단 | 임원연봉_2026.pdf | Downloads/alio/... |

## 실패
- ○○공사: 공시 미등록
```

## 하지 않는 일

- 공시 내용의 적법성·위반 여부 판단, 법령 해석
- 감사보고서 등 문서 초안 작성
- 공시 데이터에 없는 수치 추정

이런 요청은 수집 결과를 넘긴 뒤 사용자가 따로 요청하도록 안내합니다.
