"""알리오 항목별공시 MCP 서버 v0.6.0

한국 공공기관 정보공개시스템 알리오(www.alio.go.kr)의 항목별공시
92개 메뉴와 약 355개 공시 대상 기관 데이터를 LLM 도구로 노출한다.

도구 12개:
    list_menus(category, keyword)              — 메뉴 목록 (92개, v0.5.0 키워드 검색 추가)
    list_organs(rootNo, page)                  — 항목별 공시 기관 목록
    list_board_items(rootNo, apbaId, page)     — 게시판형 자료 1페이지
    list_all_board_items(rootNo, apbaId)       — 전체 페이지 자동 순회 (v0.5.0 힌트 개선)
    download_report(disclosureNo, …)           — 공시 보고서 PDF
    get_report_data(disclosureNo)              — 보고서 본문을 표·평문으로 반환 (v0.6.0)
    download_disclosure_attachment(kind, …)    — 보고서 부속 첨부 file/dfile (v0.4.0)
    search_organs(name, region, org_type)      — 기관명·지역·유형 검색 (v0.6.0 필터 추가)
    list_board_attachments(...)                — 게시판형 자료 첨부 메타
    download_board_attachment(...)             — 게시판형 첨부 다운로드
    list_rules(instName, divis, count_only,    — 기관 내부규정 목록 (v0.4.1
                include_files)                    경량옵션 추가: 다수 기관 카운트 1회 호출)
    download_rule_file(fileNo, …)              — 내부규정 파일 다운로드 (v0.4.0)

코어 라이브러리: ./alio_core.py (alio-crawler와 공유)
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from alio_core import (
    BASE_URL,
    create_session, retry_request,
    fetch_alio_items,
    fetch_report_tables,
    load_public_institutions,
    fetch_board_attachment_list, fetch_board_external_links,
    download_board_attachment as _core_download_board,
    download_attachment as _core_download_attachment,
    fetch_rule_list, fetch_all_rules, fetch_rule_detail, download_rule_file_to_path,
    fetch_all_board_items, sanitize_filename,
    RULE_DIVIS_CODES,
)

mcp = FastMCP("alio")

_JSON_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
}


def _normalize(s: str) -> str:
    return s.replace(" ", "").lower()


# ─────────────────────────────────────────────────────────────
# Tool 1: 메뉴 조회
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_menus(category: str = "", keyword: str = "") -> list[dict]:
    """알리오 항목별공시 메뉴 목록을 반환한다 (92개).

    자주 조회되는 주요 항목 (rootNo 빠른 참조):
        일반현황 10105 | 임직원 수 20201 | 임원현황 20305
        신규채용 20401 | 징계현황 21201 | 임원연봉 20501
        기관장 업무추진비 20701 | 복리후생비 20801
        이사회 43005 | 자체 감사부서 현황 32311
        감사보고서(자체감사결과) 43006 ← apbaId 필수
        감사보고서(외부) 32301 ← apbaId 필수
        감사원 지적사항 B1220-P2200 | 국회 지적사항 B1220-P2300
        국회 등 외부평가 B1210 | 경영 평가결과 B1230
        임원 모집공고 B1010 | 직원 채용정보 B1020
        입찰공고 B1030 | 내부규정 21110
        주요사업 31501 | 요약 재무상태표 31201

    Args:
        category: 대분류명. '기관운영' / 'ESG 운영' / '경영성과' / '대내외 평가 등' 등.
                  공백·대소문자 차이는 자동 흡수.
                  빈 문자열이면 대분류 필터 없음.
        keyword: 항목명 부분 일치 검색 (v0.5.0). 예: '감사', '자체감사', '징계'.
                 빈 문자열이면 키워드 필터 없음.
                 category와 동시 사용 가능 (AND 조건).

    Returns:
        메뉴 리스트. 각 항목 필드: 대분류 / 항목명 / rootNo / 보고서형
        매칭 없으면 NOT_FOUND 시그널.
    """
    items = fetch_alio_items()
    if not items:
        return [{"error": "REQUEST_FAILED: 알리오 API 응답 없음"}]

    if category:
        target = _normalize(category)
        filtered = [m for m in items if _normalize(m.get("lcdnm", "")) == target]
        if not filtered:
            return [{
                "error": f"NOT_FOUND: '{category}' 대분류 없음",
                "유효한_대분류": sorted({m["lcdnm"] for m in items if m.get("lcdnm")}),
            }]
        items = filtered

    if keyword:
        kw = keyword.strip()
        items = [m for m in items if kw in (m.get("mcdnm") or "")]
        if not items:
            return [{"error": f"NOT_FOUND: 항목명에 '{keyword}' 포함 메뉴 없음"}]

    return [
        {
            "대분류": m.get("lcdnm"),
            "항목명": m.get("mcdnm"),
            "rootNo": m.get("reportNos") or m.get("mcd"),
            "보고서형": (m.get("reportYn") or "").upper() == "Y",
        }
        for m in items
    ]


# ─────────────────────────────────────────────────────────────
# Tool 2: 항목별 공시 기관 목록
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_organs(rootNo: str, page: int = 1) -> dict:
    """특정 메뉴(rootNo)에 공시하는 기관 목록 (약 355개).

    Args:
        rootNo: 메뉴 rootNo (예: '10105' 일반현황, 'B1010' 임원 모집공고).
                콤마 다중('20201,20202,20203,20204')은 자동으로 첫 항목만 사용.
        page: 페이지 번호 (1부터).

    Returns:
        {"totalCnt", "page", "기관": [{"기관ID", "기관명", "기관유형", "주무부처",
         "기준연도", "기준분기", "공시번호", "제출번호"}, ...]}
    """
    if not rootNo:
        return {"error": "MISSING: rootNo가 필수입니다"}
    primary = rootNo.split(",")[0].strip()

    sess = create_session()
    try:
        resp = retry_request(
            sess, "POST", f"{BASE_URL}/item/itemOrganListJung.json",
            json={
                "reportFormRootNo": primary,
                "apbaType": [], "jidtDptm": [], "area": [],
                "apba_id": "", "pageNo": page,
            },
            headers=_JSON_HEADERS, timeout=15,
        )
        body = resp.json()
    except Exception as e:
        return {"error": f"REQUEST_FAILED: {e}"}

    if body.get("status") and body.get("status") != "success":
        return {
            "error": "ALIO_API_FAIL", "rootNo": primary,
            "message": body.get("message", "알 수 없음"),
            "hint": "일부 rootNo는 알리오 자체 결함(예: 63601 주요기관 상세부채). 다른 rootNo 시도 또는 재시도.",
        }

    d = body.get("data") or {}
    organs = d.get("organList", []) or []
    if not organs:
        return {"error": f"NOT_FOUND: rootNo='{primary}' 기관 목록 없음"}

    return {
        "totalCnt": d.get("totalCnt"),
        "page": page,
        "기관": [
            {
                "기관ID": o.get("apbaId"),
                "기관명": o.get("apbaNa"),
                "기관유형": o.get("typeNa"),
                "주무부처": o.get("jidtNa"),
                "기준연도": o.get("critYyyy"),
                "기준분기": o.get("quartNa"),
                "공시번호": o.get("disclosureNo"),
                "제출번호": o.get("submissionNo"),
            }
            for o in organs
        ],
    }


# ─────────────────────────────────────────────────────────────
# Tool 3: 게시판형 자료 목록
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_board_items(rootNo: str, apbaId: str = "", page: int = 1) -> dict:
    """게시판형·보고서형 항목의 자료 목록 (1페이지 = 최대 10건).

    게시판형 12종: B1010 임원 모집공고, B1020 직원 채용정보, B1030 입찰공고,
    B1210 국회 등 외부평가, B1220 감사원 지적사항 등.
    보고서형(숫자 rootNo): 43006 자체감사결과, 32301 감사보고서 등.

    **중요**: 43006·32301 등 일부 rootNo는 apbaId 없이 전체 조회 불가.

    Args:
        rootNo: 항목 rootNo (예: 'B1010', '43006').
        apbaId: 특정 기관 ID로 한정 (예: 'C0208').
                'B' 시작 게시판형은 빈 문자열로 전체 조회 가능.
                숫자 rootNo(43006 등)는 apbaId 필수일 수 있음.
        page: 페이지 번호 (1부터).

    Returns:
        {"rootNo", "page", "자료": [{"제목", "등록일", "기관ID", "공시번호",
         "제출번호", "idx", "reportFormNo", "tableName", "idxName", "bidType"}, ...]}
        자료 응답 필드 그대로 list_board_attachments 호출에 활용 가능.
    """
    if not rootNo:
        return {"error": "MISSING: rootNo가 필수입니다"}

    sess = create_session()
    try:
        resp = retry_request(
            sess, "POST", f"{BASE_URL}/item/itemReportListSusi.json",
            json={
                "pageNo": page, "apbaId": apbaId, "apbaType": "",
                "reportFormRootNo": rootNo, "search_word": "",
                "search_flag": "title", "bid_type": "", "enfc_istt": "",
            },
            headers=_JSON_HEADERS, timeout=15,
        )
        body = resp.json()
    except Exception as e:
        return {"error": f"REQUEST_FAILED: {e}"}

    if body.get("status") and body.get("status") != "success":
        return {
            "error": "ALIO_API_FAIL", "rootNo": rootNo, "apbaId": apbaId,
            "message": body.get("message", "알 수 없음"),
        }

    d = body.get("data") or {}
    items = d.get("result", []) or []
    if not items:
        return {"error": f"NOT_FOUND: rootNo='{rootNo}' apbaId='{apbaId}' 자료 없음"}

    return {
        "rootNo": rootNo, "page": page,
        "자료": [
            {
                "제목": v.get("title"), "등록일": v.get("idate"),
                "기관ID": v.get("apbaId"),
                "공시번호": v.get("disclosureNo"),
                "제출번호": v.get("submissionNo"),
                "idx": v.get("idx"),
                "reportFormNo": v.get("reportFormNo"),
                "tableName": v.get("tableName"),
                "idxName": v.get("idxName"),
                "bidType": v.get("bidType"),
            }
            for v in items
        ],
    }


# ─────────────────────────────────────────────────────────────
# Tool 4: 공시 보고서 PDF 다운로드
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def download_report(
    disclosureNo: str,
    save_dir: str = "/tmp/alio_downloads",
    filename: str = "",
) -> dict:
    """공시번호로 보고서 PDF 다운로드.

    보고서형 메뉴(임직원수·일반현황·자체 감사부서 현황 등)의 공시번호로 호출.

    Args:
        disclosureNo: 공시번호 (list_organs/list_board_items 응답의 '공시번호').
        save_dir: 저장 디렉토리 (없으면 자동 생성).
        filename: 저장 파일명. 빈 문자열이면 'alio_{disclosureNo}.pdf'.

    Returns:
        {"saved_path", "size_bytes"}
    """
    if not disclosureNo:
        return {"error": "MISSING: disclosureNo가 필수입니다"}

    Path(save_dir).mkdir(parents=True, exist_ok=True)
    fn = filename or f"alio_{disclosureNo}.pdf"

    sess = create_session()
    success, saved, msg = _core_download_attachment(
        sess, "pdf",
        {"name": fn},
        save_dir,
        disclosure_no=disclosureNo,
    )
    if not success:
        return {"error": f"DOWNLOAD_FAILED: {msg}", "disclosureNo": disclosureNo}
    return {
        "saved_path": saved,
        "size_bytes": os.path.getsize(saved) if os.path.exists(saved) else 0,
    }


# ─────────────────────────────────────────────────────────────
# Tool 5: 기관명 부분 일치 검색 (신규 v0.3.0)
# ─────────────────────────────────────────────────────────────
_INST_CACHE: dict = {}


@mcp.tool()
def search_organs(name: str = "", region: str = "", org_type: str = "") -> dict:
    """공공기관 약 355개를 기관명·지역·기관유형으로 검색 (부분 일치, AND).

    첫 호출 시 알리오 기관목록 API를 1회 호출해 캐시. 이후는 메모리에서 즉시 검색.
    세 인자는 모두 부분 문자열 매칭이며, 함께 주면 AND로 좁혀진다.
    셋 다 비우면 에러.

    Args:
        name: 기관명 부분 문자열. 예: '산업단지', '한국전력'.
        region: 소재지(본사) 부분 문자열. 예: '대구', '대구광역시', '세종'.
        org_type: 기관유형 부분 문자열. 예: '위탁집행', '준정부기관(위탁집행형)',
                  '공기업', '기금관리', '기타공공기관'.

    Returns:
        {"총_검색결과", "조건", "기관": [{"기관ID", "기관명", "기관유형",
         "주무부처", "지역"}, ...]}  상위 50건 한정.

    예) search_organs(region='대구', org_type='위탁집행')
        → 대구 소재 위탁집행형 준정부기관 일괄 조회
    """
    name = (name or "").strip()
    region = (region or "").strip()
    org_type = (org_type or "").strip()
    if not (name or region or org_type):
        return {"error": "MISSING: name·region·org_type 중 최소 하나가 필요합니다"}

    global _INST_CACHE
    if not _INST_CACHE:
        _INST_CACHE = load_public_institutions()
        if not _INST_CACHE:
            return {"error": "기관 목록 로드 실패"}

    matches = [
        {
            "기관ID": v["apba_id"],
            "기관명": inst_name,
            "기관유형": v["inst_type"],
            "주무부처": v["dept"],
            "지역": v["region"],
        }
        for inst_name, v in _INST_CACHE.items()
        if (not name or name in inst_name)
        and (not region or region in (v.get("region") or ""))
        and (not org_type or org_type in (v.get("inst_type") or ""))
    ]

    if not matches:
        return {
            "error": "NOT_FOUND: 조건에 맞는 기관 없음",
            "총_검색결과": 0,
            "조건": {"name": name, "region": region, "org_type": org_type},
        }

    return {
        "총_검색결과": len(matches),
        "조건": {"name": name, "region": region, "org_type": org_type},
        "기관": matches[:50],
    }


# ─────────────────────────────────────────────────────────────
# Tool 12: 보고서 본문(표·평문) 조회 — itemReportRight.do (신규 v0.6.0)
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def get_report_data(disclosureNo: str) -> dict:
    """보고서형 공시의 본문을 표·평문 텍스트로 반환 (PDF/HWP 우회).

    download_report는 PDF를 저장만 하고 본문을 돌려주지 않는다. 이 도구는
    itemReportRight.do의 HTML 표를 파싱해 LLM이 바로 읽을 수 있는 행렬·평문으로
    반환한다. 징계현황·임직원수·복리후생비·임원현황 등 보고서형 항목의 실제
    내용을 파일 다운로드 없이 확인할 때 사용한다.

    공시번호는 list_organs(rootNo) 또는 list_board_items 응답의 '공시번호'.

    Args:
        disclosureNo: 공시번호.

    Returns:
        {"disclosureNo", "제목", "표_개수", "표": [[셀,...],...], "본문텍스트"}
        본문이 비면 EMPTY 에러 (순수 첨부 항목 — download_* 사용 안내).
    """
    if not disclosureNo:
        return {"error": "MISSING: disclosureNo가 필수입니다"}
    session = create_session()
    return fetch_report_tables(session, disclosureNo)


# ─────────────────────────────────────────────────────────────
# Tool 6: 게시판형 자료 첨부파일 메타 (신규 v0.3.0)
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_board_attachments(
    apbaId: str,
    reportFormNo: str,
    idx: str = "",
    disclosureNo: str = "",
    tableName: str = "",
    idxName: str = "",
    bidType: str = "",
) -> dict:
    """게시판형 자료의 첨부파일·외부링크 메타를 itemBoard{reportFormNo}.do HTML에서 추출.

    list_board_items 응답의 한 자료에 대해 호출. 인자는 list_board_items 응답에서
    그대로 전달하면 된다 (apbaId, reportFormNo, idx, 공시번호 → disclosureNo,
    tableName, idxName, bidType).

    두 가지 첨부 패턴 통합:
    - kind="upload": ``/upload{spath}{sfile}`` 직접 GET (B1220 감사원 지적사항 등)
    - kind="fileno": ``/download/download.json?fileNo=N`` GET (B1010 임원 모집공고 등)

    Args:
        apbaId: 기관ID (필수, 예: 'C0208').
        reportFormNo: 게시판형 항목 식별자 (필수, 예: 'B1220').
        idx: list_board_items 응답의 'idx'.
        disclosureNo: list_board_items 응답의 '공시번호'.
        tableName, idxName, bidType: list_board_items 응답에서 그대로 전달.

    Returns:
        {
          "첨부": [{"kind", "name", "spath", "sfile", "file_no"}, ...],
          "외부링크": [{"url", "text"}, ...]
        }
        download_board_attachment에 첨부 항목 필드를 그대로 전달해 받는다.
    """
    if not apbaId or not reportFormNo:
        return {"error": "MISSING: apbaId, reportFormNo 필수"}

    sess = create_session()
    violation_meta = {
        "report_form_no": reportFormNo,
        "disclosure_no": disclosureNo,
        "idx": idx,
        "table_name": tableName,
        "idx_name": idxName,
        "bid_type": bidType,
    }

    attachments = fetch_board_attachment_list(sess, apbaId, violation_meta)
    ext_links = fetch_board_external_links(sess, apbaId, violation_meta)

    if not attachments and not ext_links:
        return {
            "error": "NOT_FOUND: 첨부 또는 외부링크 없음",
            "첨부": [], "외부링크": [],
        }

    return {
        "첨부": [
            {
                "kind": a["kind"],
                "name": a.get("dfile", ""),
                "spath": a.get("spath", ""),
                "sfile": a.get("sfile", ""),
                "file_no": a.get("file_no", ""),
            }
            for a in attachments
        ],
        "외부링크": ext_links,
    }


# ─────────────────────────────────────────────────────────────
# Tool 7: 게시판형 첨부파일 다운로드 (신규 v0.3.0)
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def download_board_attachment(
    kind: str,
    name: str = "",
    spath: str = "",
    sfile: str = "",
    file_no: str = "",
    save_dir: str = "/tmp/alio_downloads",
) -> dict:
    """게시판형 첨부파일 다운로드.

    list_board_attachments 응답의 '첨부' 항목 필드를 그대로 전달.

    Args:
        kind: 'upload' (spath/sfile 사용) 또는 'fileno' (file_no 사용).
        name: 저장 파일명 (옵션, 미지정 시 자동 명명).
        spath: kind='upload'일 때 알리오 upload 경로.
        sfile: kind='upload'일 때 알리오 파일명.
        file_no: kind='fileno'일 때 알리오 fileNo.
        save_dir: 저장 디렉토리 (없으면 자동 생성).

    Returns:
        {"saved_path", "size_bytes"}
    """
    if kind not in ("upload", "fileno"):
        return {"error": f"INVALID: kind는 'upload' 또는 'fileno' (받음: '{kind}')"}
    if kind == "upload" and (not spath or not sfile):
        return {"error": "MISSING: kind='upload'는 spath, sfile 필수"}
    if kind == "fileno" and not file_no:
        return {"error": "MISSING: kind='fileno'는 file_no 필수"}

    Path(save_dir).mkdir(parents=True, exist_ok=True)
    sess = create_session()
    attachment = {
        "kind": kind, "dfile": name,
        "spath": spath, "sfile": sfile, "file_no": file_no,
    }
    success, saved, msg = _core_download_board(sess, attachment, save_dir)
    if not success:
        return {"error": f"DOWNLOAD_FAILED: {msg}"}
    return {
        "saved_path": saved,
        "size_bytes": os.path.getsize(saved) if os.path.exists(saved) else 0,
    }


# ─────────────────────────────────────────────────────────────
# Tool 8: 게시판형 자료 전체 페이지 자동 순회 (신규 v0.4.0)
# ─────────────────────────────────────────────────────────────
_APBA_REQUIRED_ROOTS = {"43006", "32301"}

@mcp.tool()
def list_all_board_items(rootNo: str, apbaId: str = "") -> dict:
    """itemReportListSusi 응답을 모든 페이지에 걸쳐 자동 순회한다.

    list_board_items는 단일 페이지(통상 10건). 자체감사·경영실적평가·
    감사원 지적사항처럼 누적 수십~수백건이 쌓이는 자료군은 한 번에
    전부 조회하는 것이 더 효율적이다.

    **중요**: 43006(자체감사결과), 32301(감사보고서) 등 일부 rootNo는
    알리오 API 특성상 apbaId(기관ID) 없이 전체 조회가 불가능하다.
    이 경우 반드시 apbaId를 지정해야 한다.
    기관ID는 search_organs(name) 또는 list_organs(rootNo)로 확인.

    Args:
        rootNo: 자료 식별자. 예 — '43006'(자체감사결과), 'B1230'(경영실적
                평가결과), 'B1220'(감사원 지적사항), 'B1210'(국회 외부평가).
        apbaId: 기관 ID. 'B'로 시작하는 게시판형은 빈 문자열로 전체 기관
                조회 가능. 숫자 rootNo(43006 등)는 apbaId 필수.

    Returns:
        {"rootNo", "totalCnt", "자료": [{"제목", "등록일", "기관ID",
         "공시번호", "제출번호", "idx", "reportFormNo", "tableName",
         "idxName", "bidType"}, ...]}
        list_board_attachments 호출에 자료 필드를 그대로 전달 가능.
    """
    if not rootNo:
        return {"error": "MISSING: rootNo가 필수입니다"}

    primary = rootNo.split(",")[0].strip()
    if not apbaId and primary in _APBA_REQUIRED_ROOTS:
        return {
            "error": f"APBA_REQUIRED: rootNo='{primary}'는 apbaId(기관ID) 없이 전체 조회 불가",
            "hint": "search_organs(name)으로 기관ID를 먼저 확인한 뒤 apbaId를 지정하세요.",
            "totalCnt": 0, "자료": [],
        }

    sess = create_session()
    items = fetch_all_board_items(sess, rootNo, apba_id=apbaId)
    if not items:
        hint = ""
        if not apbaId and not primary.startswith("B"):
            hint = "이 rootNo는 apbaId 필수일 수 있음. search_organs로 기관ID 확인 후 재시도."
        result = {
            "error": f"NOT_FOUND: rootNo='{rootNo}' apbaId='{apbaId}' 자료 없음",
            "totalCnt": 0, "자료": [],
        }
        if hint:
            result["hint"] = hint
        return result
    return {
        "rootNo": rootNo,
        "totalCnt": len(items),
        "자료": items,
    }


# ─────────────────────────────────────────────────────────────
# Tool 9: 보고서형 부속 첨부 다운로드 file·dfile (신규 v0.4.0)
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def download_disclosure_attachment(
    kind: str,
    fileName: str,
    disclosureNo: str = "",
    submissionNo: str = "",
    fileId: str = "",
    save_dir: str = "/tmp/alio_downloads",
) -> dict:
    """보고서형 공시의 부속 첨부파일 다운로드.

    download_report(공시 PDF)와 달리 보고서에 동봉된 엑셀·한글 등 부속 첨부.

    Args:
        kind: 'file'(일반 첨부 — fileId + disclosureNo 필요) 또는
              'dfile'(안전경영책임보고서 — fileName + submissionNo 필요,
              사망자수 공시 70401에 해당).
        fileName: 저장 파일명 + 'dfile' 식별자. 알리오 응답에서 받은 원본명.
        disclosureNo: 공시번호 (kind='file'일 때 필수).
        submissionNo: 제출번호 (kind='dfile'일 때 필수).
        fileId: 파일 ID (kind='file'일 때 필수, parse_files_field 결과의 'id').
        save_dir: 저장 디렉토리 (없으면 자동 생성).

    Returns:
        {"saved_path", "size_bytes"}
    """
    if kind not in ("file", "dfile"):
        return {"error": f"INVALID: kind는 'file' 또는 'dfile' (받음: '{kind}')"}
    if not fileName:
        return {"error": "MISSING: fileName이 필수입니다"}
    if kind == "file" and (not fileId or not disclosureNo):
        return {"error": "MISSING: kind='file'은 fileId + disclosureNo 필수"}
    if kind == "dfile" and not submissionNo:
        return {"error": "MISSING: kind='dfile'은 submissionNo 필수"}

    Path(save_dir).mkdir(parents=True, exist_ok=True)
    sess = create_session()
    success, saved, msg = _core_download_attachment(
        sess, kind,
        {"id": fileId, "name": fileName},
        save_dir,
        disclosure_no=disclosureNo,
        submission_no=submissionNo,
    )
    if not success:
        return {"error": f"DOWNLOAD_FAILED: {msg}"}
    return {
        "saved_path": saved,
        "size_bytes": os.path.getsize(saved) if os.path.exists(saved) else 0,
    }


# ─────────────────────────────────────────────────────────────
# Tool 10: 기관 내부규정 목록 + 최신 파일 메타 (신규 v0.4.0)
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_rules(
    instName: str,
    divis: str = "",
    count_only: bool = False,
    include_files: bool = True,
) -> dict:
    """기관 내부규정 목록 조회 (성능 옵션 2종).

    기본은 전체 페이지 순회 + 각 규정의 findRuleDtl 호출로 최신 파일 메타까지
    반환한다 (다운로드 체인용). 다수 기관 카운트 집계 등 빠른 호출이 필요하면
    count_only=True / include_files=False로 호출 횟수를 크게 줄일 수 있다.

    Args:
        instName: 기관명 (apbaNa 검색). 예: '한국산업단지공단', '한국전력공사'.
        divis: 분류 코드. 빈 문자열이면 전체.
               'K1500'(정관), 'K1100'(인사·복무·징계), 'K1200'(보수),
               'K1300'(직제), 'K1400'(기타).
        count_only: True면 findRuleList 1페이지만 호출해 totalCnt만 반환.
                    findRuleDtl·후속 페이지 호출 일체 없음. 다수 기관 카운트
                    집계용 최고속 모드 (HTTP 1회).
        include_files: False면 findRuleDtl 호출을 생략한다(파일 메타 없음).
                       페이지 순회만 수행 → 호출 횟수를 페이지 수만큼으로 절감.
                       다운로드가 필요 없는 목록 조회에 권장.

    Returns:
        count_only=True:
            {"instName", "totalCnt", "분류명"}
        count_only=False, include_files=False:
            {"totalCnt", "분류명", "규정": [{"seq", "title", "insdRuleDivis"}, ...]}
        count_only=False, include_files=True (기본 — 풀스펙):
            {"totalCnt", "분류명", "규정": [{"seq", "title", "insdRuleDivis",
             "files_count", "latest": {"file_no", "file_name"}}, ...]}

    호출 횟수 비교 (한국국토정보공사 158건 기준):
        count_only=True             → 1회
        include_files=False         → 16회 (페이지 수만)
        기본(include_files=True)    → 174회 (페이지 16 + findRuleDtl 158)
    """
    if not instName:
        return {"error": "MISSING: instName이 필수입니다"}
    if divis and divis not in RULE_DIVIS_CODES.values():
        return {
            "error": f"INVALID: divis는 RULE_DIVIS_CODES 값 중 하나여야 함",
            "유효한_divis": RULE_DIVIS_CODES,
        }

    sess = create_session()
    divis_label = next((k for k, v in RULE_DIVIS_CODES.items() if v == divis), "전체")

    if count_only:
        first = fetch_rule_list(sess, instName, divis=divis, page=1)
        if "error" in first and not first.get("result"):
            return {
                "error": f"FETCH_FAILED: {first.get('error')}",
                "instName": instName,
                "totalCnt": 0,
                "분류명": divis_label,
            }
        return {
            "instName": instName,
            "totalCnt": first.get("totalCnt", 0),
            "분류명": divis_label,
        }

    rules = fetch_all_rules(sess, instName, divis=divis)
    if not rules:
        return {"error": f"NOT_FOUND: '{instName}' 내부규정 없음", "totalCnt": 0, "규정": []}

    if not include_files:
        result = [
            {
                "seq": r.get("seq", ""),
                "title": r.get("title", ""),
                "insdRuleDivis": r.get("insdRuleDivis", ""),
            }
            for r in rules
        ]
        return {
            "totalCnt": len(result),
            "분류명": divis_label,
            "규정": result,
        }

    result = []
    for r in rules:
        seq = r.get("seq", "")
        detail = fetch_rule_detail(sess, seq) if seq else {"files": [], "latest": None}
        result.append({
            "seq": seq,
            "title": r.get("title", ""),
            "insdRuleDivis": r.get("insdRuleDivis", ""),
            "files_count": len(detail.get("files", [])),
            "latest": detail.get("latest"),
        })

    return {
        "totalCnt": len(result),
        "분류명": divis_label,
        "규정": result,
    }


# ─────────────────────────────────────────────────────────────
# Tool 11: 내부규정 파일 다운로드 (신규 v0.4.0)
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def download_rule_file(
    fileNo: str,
    fileName: str = "",
    save_dir: str = "/tmp/alio_downloads",
) -> dict:
    """내부규정 파일을 fileNo로 단건 다운로드.

    list_rules 응답의 'latest.file_no' / 'latest.file_name'을 그대로 전달.

    Args:
        fileNo: 알리오 fileNo (list_rules → latest.file_no).
        fileName: 저장 파일명. 빈 문자열이면 'rule_{fileNo}.bin'.
        save_dir: 저장 디렉토리 (없으면 자동 생성).

    Returns:
        {"saved_path", "size_bytes"}
    """
    if not fileNo:
        return {"error": "MISSING: fileNo가 필수입니다"}

    Path(save_dir).mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(fileName or f"rule_{fileNo}.bin", max_len=120)
    save_path = os.path.join(save_dir, safe_name)

    sess = create_session()
    success, saved, msg = download_rule_file_to_path(sess, fileNo, save_path)
    if not success:
        return {"error": f"DOWNLOAD_FAILED: {msg}", "fileNo": fileNo}
    return {
        "saved_path": saved,
        "size_bytes": os.path.getsize(saved) if os.path.exists(saved) else 0,
    }


# ─────────────────────────────────────────────────────────────
# 서버 실행
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
