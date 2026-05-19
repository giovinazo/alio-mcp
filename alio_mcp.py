"""알리오 항목별공시 MCP 서버 v0.4.0

한국 공공기관 정보공개시스템 알리오(www.alio.go.kr)의 항목별공시
92개 메뉴와 약 355개 공시 대상 기관 데이터를 LLM 도구로 노출한다.

도구 11개:
    list_menus(category)                       — 메뉴 목록 (92개)
    list_organs(rootNo, page)                  — 항목별 공시 기관 목록
    list_board_items(rootNo, apbaId, page)     — 게시판형 자료 1페이지
    list_all_board_items(rootNo, apbaId)       — 전체 페이지 자동 순회 (신규 v0.4.0)
    download_report(disclosureNo, …)           — 공시 보고서 PDF
    download_disclosure_attachment(kind, …)    — 보고서 부속 첨부 file/dfile (신규 v0.4.0)
    search_organs(name)                        — 기관명 부분 일치 검색
    list_board_attachments(...)                — 게시판형 자료 첨부 메타
    download_board_attachment(...)             — 게시판형 첨부 다운로드
    list_rules(instName, divis)                — 기관 내부규정 목록 + 최신 파일 (신규 v0.4.0)
    download_rule_file(fileNo, …)              — 내부규정 파일 다운로드 (신규 v0.4.0)

코어 라이브러리: ./alio_core.py (alio-crawler와 공유)
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from alio_core import (
    BASE_URL,
    create_session, retry_request,
    fetch_alio_items,
    load_public_institutions,
    fetch_board_attachment_list, fetch_board_external_links,
    download_board_attachment as _core_download_board,
    download_attachment as _core_download_attachment,
    fetch_all_rules, fetch_rule_detail, download_rule_file_to_path,
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
def list_menus(category: str = "") -> list[dict]:
    """알리오 항목별공시 메뉴 목록을 반환한다 (92개, v5.4.2).

    Args:
        category: 대분류명. '기관운영' / 'ESG 운영' / '경영성과' / '대내외 평가 등' / 'AI 활용' 등.
                  공백·대소문자 차이는 자동 흡수.
                  빈 문자열이면 전체 메뉴 반환.

    Returns:
        메뉴 리스트. 각 항목 필드: 대분류 / 항목명 / rootNo / 보고서형
        매칭 없으면 NOT_FOUND 시그널 + 유효한 대분류 목록 반환.
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
    """게시판형(보고서형=False) 항목의 자료 목록.

    게시판형 12종: B1010 임원 모집공고, B1020 직원 채용정보, B1030 입찰공고,
    B1210 국회 등 외부평가, B1220 감사원 지적사항 등.

    Args:
        rootNo: 게시판형 항목 rootNo (예: 'B1010').
        apbaId: 특정 기관 ID로 한정 (예: 'C0208'). 빈 문자열이면 전체 기관 최근순.
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
def search_organs(name: str) -> dict:
    """공공기관 약 355개 중 기관명 부분 일치 검색.

    첫 호출 시 알리오 기관목록 API를 1회 호출해 캐시. 이후는 메모리에서 즉시 검색.

    Args:
        name: 검색 키워드 (부분 문자열). 예: '산업단지', '한국전력'.

    Returns:
        {"총_검색결과", "기관": [{"기관ID", "기관명", "기관유형", "주무부처", "지역"}, ...]}
        상위 50건 한정.
    """
    if not name:
        return {"error": "MISSING: name이 필수입니다"}

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
        if name.strip() in inst_name
    ]

    if not matches:
        return {"error": f"NOT_FOUND: '{name}' 포함 기관 없음", "총_검색결과": 0}

    return {
        "총_검색결과": len(matches),
        "기관": matches[:50],
    }


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
@mcp.tool()
def list_all_board_items(rootNo: str, apbaId: str = "") -> dict:
    """itemReportListSusi 응답을 모든 페이지에 걸쳐 자동 순회한다.

    list_board_items는 단일 페이지(통상 10건). 자체감사·경영실적평가·
    감사원 지적사항처럼 누적 수십~수백건이 쌓이는 자료군은 한 번에
    전부 조회하는 것이 더 효율적이다.

    Args:
        rootNo: 자료 식별자. 예 — '43006'(자체감사), 'B1230'(경영실적
                평가결과), 'B1220'(감사원 지적사항), 'B1210'(국회 외부평가).
        apbaId: 기관 ID. 빈 문자열이면 전체 기관 최근순.

    Returns:
        {"rootNo", "totalCnt", "자료": [{"제목", "등록일", "기관ID",
         "공시번호", "제출번호", "idx", "reportFormNo", "tableName",
         "idxName", "bidType"}, ...]}
        list_board_attachments 호출에 자료 필드를 그대로 전달 가능.
    """
    if not rootNo:
        return {"error": "MISSING: rootNo가 필수입니다"}

    sess = create_session()
    items = fetch_all_board_items(sess, rootNo, apba_id=apbaId)
    if not items:
        return {
            "error": f"NOT_FOUND: rootNo='{rootNo}' apbaId='{apbaId}' 자료 없음",
            "totalCnt": 0, "자료": [],
        }
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
def list_rules(instName: str, divis: str = "") -> dict:
    """기관 내부규정 목록을 전체 페이지 자동 순회로 조회.

    각 규정에 대해 findRuleDtl을 호출해 .zip을 제외한 **최신 파일 메타**까지
    함께 반환한다. 반환된 latest.file_no를 download_rule_file에 그대로 전달
    가능.

    Args:
        instName: 기관명 (apbaNa 검색). 예: '한국산업단지공단', '한국전력공사'.
        divis: 분류 코드. 빈 문자열이면 전체.
               'K1500'(정관), 'K1100'(인사·복무·징계), 'K1200'(보수),
               'K1300'(직제), 'K1400'(기타).

    Returns:
        {"totalCnt", "분류명": divis_label, "규정": [{"seq", "title",
         "insdRuleDivis", "files_count", "latest": {"file_no", "file_name"}}, ...]}
    """
    if not instName:
        return {"error": "MISSING: instName이 필수입니다"}
    if divis and divis not in RULE_DIVIS_CODES.values():
        return {
            "error": f"INVALID: divis는 RULE_DIVIS_CODES 값 중 하나여야 함",
            "유효한_divis": RULE_DIVIS_CODES,
        }

    sess = create_session()
    rules = fetch_all_rules(sess, instName, divis=divis)
    if not rules:
        return {"error": f"NOT_FOUND: '{instName}' 내부규정 없음", "totalCnt": 0, "규정": []}

    divis_label = next((k for k, v in RULE_DIVIS_CODES.items() if v == divis), "전체")
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
