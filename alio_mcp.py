"""알리오 항목별공시 MCP 서버

한국 공공기관 정보공개시스템 알리오(www.alio.go.kr)의 항목별공시
83개 메뉴와 약 344개 공시 대상 기관 데이터를 LLM 도구로 노출한다.

도구 4개:
    list_menus(category)                    — 메뉴 목록 조회 (대분류 필터)
    list_organs(rootNo, page)               — 항목별 공시 기관 목록
    list_board_items(rootNo, apbaId, page)  — 게시판형 자료 목록
    download_report(disclosureNo, save_dir) — 공시 보고서 PDF 다운로드
"""

from mcp.server.fastmcp import FastMCP
from pathlib import Path
import os
import requests

mcp = FastMCP("alio")

BASE_URL = "https://www.alio.go.kr"
JSON_HEADERS = {"Content-Type": "application/json;charset=UTF-8"}

VALID_CATEGORIES = ["기관운영", "ESG 운영", "경영성과", "대내외 평가 등"]


def _normalize(s: str) -> str:
    return s.replace(" ", "").lower()


# ─────────────────────────────────────────────────────────────
# Tool 1: 메뉴 조회
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_menus(category: str = "") -> list[dict]:
    """알리오 항목별공시 메뉴 목록을 반환한다.

    Args:
        category: 대분류명. '기관운영' / 'ESG 운영' / '경영성과' / '대내외 평가 등' 중 하나.
                  공백·대소문자 차이는 자동 흡수된다.
                  빈 문자열이면 전체 83개 메뉴를 반환한다.

    Returns:
        메뉴 리스트. 각 항목은 다음 필드 포함:
            - 대분류: 4개 카테고리 중 하나
            - 항목명: 메뉴 이름 (예: '임직원 수')
            - rootNo: 알리오 내부 메뉴 식별자 (콤마 구분 다중 가능)
            - 보고서형: True=정형 보고서, False=게시판형
        매칭 없으면 NOT_FOUND 시그널과 유효한 대분류 목록을 반환한다.
    """
    try:
        resp = requests.post(
            f"{BASE_URL}/item/formList.json",
            json={}, headers=JSON_HEADERS, timeout=10,
        )
        body = resp.json()
    except Exception as e:
        return [{"error": f"REQUEST_FAILED: {e}"}]

    if body.get("status") and body.get("status") != "success":
        return [{
            "error": f"ALIO_API_FAIL: {body.get('message', '알 수 없음')}",
        }]
    data = body.get("data") or []

    if category:
        target = _normalize(category)
        data = [m for m in data if _normalize(m["lcdnm"]) == target]
        if not data:
            return [{
                "error": f"NOT_FOUND: '{category}' 대분류 없음",
                "유효한_대분류": VALID_CATEGORIES,
            }]

    return [
        {
            "대분류": m["lcdnm"],
            "항목명": m["mcdnm"],
            "rootNo": m["reportNos"],
            "보고서형": m["reportYn"] == "Y",
        }
        for m in data
    ]


# ─────────────────────────────────────────────────────────────
# Tool 2: 항목별 공시 기관 목록
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def list_organs(rootNo: str, page: int = 1) -> dict:
    """알리오 항목별공시 — 특정 메뉴(rootNo)에 공시하는 기관 목록을 반환한다.

    공공기관 약 344개의 기관ID·기관명·기관유형·주무부처를 한 번에 받아올 수 있어,
    이후 `list_board_items` 또는 `download_report` 호출 시 기관 식별자(apbaId)로 사용한다.

    Args:
        rootNo: 메뉴 rootNo (예: '10105' 일반현황, '20201' 임직원수 1분기,
                'B1010' 임원 모집공고).
                여러 rootNo가 콤마로 묶인 메뉴(예: '20201,20202,20203,20204')는
                자동으로 첫 항목만 사용한다.
        page: 페이지 번호 (1부터). 기본값 1.

    Returns:
        {
          "totalCnt": 전체 기관 수,
          "page": 현재 페이지,
          "기관": [
            {"기관ID", "기관명", "기관유형", "주무부처",
             "기준연도", "기준분기", "공시번호", "제출번호"},
            ...
          ]
        }
        에러 시 {"error": "..."} 반환.
    """
    if not rootNo:
        return {"error": "MISSING: rootNo가 필수입니다"}

    primary = rootNo.split(",")[0].strip()

    try:
        resp = requests.post(
            f"{BASE_URL}/item/itemOrganListJung.json",
            json={
                "reportFormRootNo": primary,
                "apbaType": [], "jidtDptm": [], "area": [],
                "apba_id": "", "pageNo": page,
            },
            headers=JSON_HEADERS, timeout=15,
        )
        body = resp.json()
    except Exception as e:
        return {"error": f"REQUEST_FAILED: {e}"}

    # 알리오 서버 status 검증 (일부 rootNo는 'fail' 반환 -- 예: 63601 주요기관 상세부채정보)
    if body.get("status") and body.get("status") != "success":
        return {
            "error": "ALIO_API_FAIL",
            "rootNo": primary,
            "message": body.get("message", "알 수 없음"),
            "hint": "해당 항목은 알리오 사이트 자체 결함일 수 있습니다. 다른 rootNo를 시도하거나 잠시 후 재시도하십시오.",
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
    """알리오 게시판형(보고서형=False) 항목의 자료 목록을 반환한다.

    게시판형 12종(B1010 임원 모집공고, B1020 직원 채용정보, B1030 입찰공고,
    B1210 국회 등 외부평가, B1220 감사원 지적사항 등)에 사용한다.

    Args:
        rootNo: 게시판형 항목 rootNo (예: 'B1010' 임원 모집공고).
        apbaId: 특정 기관 ID로 한정 (예: 'C0208' 한국산업단지공단).
                빈 문자열이면 전체 기관의 자료가 등록일 최근순으로 섞여 반환된다.
        page: 페이지 번호 (1부터). 기본값 1.

    Returns:
        {
          "rootNo": "B1010",
          "page": 1,
          "자료": [
            {"제목", "등록일", "기관ID", "공시번호", "제출번호",
             "idx", "reportFormNo", "tableName", "idxName", "bidType"},
            ...
          ]
        }
        에러 시 {"error": "..."} 반환.
    """
    if not rootNo:
        return {"error": "MISSING: rootNo가 필수입니다"}

    try:
        resp = requests.post(
            f"{BASE_URL}/item/itemReportListSusi.json",
            json={
                "pageNo": page,
                "apbaId": apbaId,
                "apbaType": "",
                "reportFormRootNo": rootNo,
                "search_word": "",
                "search_flag": "title",
                "bid_type": "",
                "enfc_istt": "",
            },
            headers=JSON_HEADERS, timeout=15,
        )
        body = resp.json()
    except Exception as e:
        return {"error": f"REQUEST_FAILED: {e}"}

    # 알리오 서버 status 검증
    if body.get("status") and body.get("status") != "success":
        return {
            "error": "ALIO_API_FAIL",
            "rootNo": rootNo,
            "apbaId": apbaId,
            "message": body.get("message", "알 수 없음"),
        }

    d = body.get("data") or {}
    items = d.get("result", []) or []

    if not items:
        return {
            "error": f"NOT_FOUND: rootNo='{rootNo}' apbaId='{apbaId}' 자료 없음"
        }

    return {
        "rootNo": rootNo,
        "page": page,
        "자료": [
            {
                "제목": v.get("title"),
                "등록일": v.get("idate"),
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
    """알리오 공시 보고서 PDF를 다운로드해 로컬에 저장한다.

    보고서형 메뉴(예: 임직원 수, 일반현황, 자체 감사부서 현황 등)의
    공시번호(disclosureNo)로 호출하면 /download/pdf.json 엔드포인트에서
    PDF를 받아 지정 디렉토리에 저장한다.

    Args:
        disclosureNo: 공시번호 (list_organs 또는 list_board_items 응답의 '공시번호').
        save_dir: 저장 디렉토리. 기본 '/tmp/alio_downloads' (없으면 자동 생성).
        filename: 저장 파일명(확장자 포함). 빈 문자열이면 'alio_{disclosureNo}.pdf'로 자동 명명.

    Returns:
        {
          "saved_path": "/저장된/절대/경로.pdf",
          "size_bytes": 90291,
          "content_type": "application/pdf"
        }
        에러 시 {"error": "..."} 반환.
    """
    if not disclosureNo:
        return {"error": "MISSING: disclosureNo가 필수입니다"}

    try:
        resp = requests.get(
            f"{BASE_URL}/download/pdf.json",
            params={"disclosureNo": disclosureNo},
            timeout=30,
        )
    except Exception as e:
        return {"error": f"REQUEST_FAILED: {e}"}

    if resp.status_code != 200:
        return {
            "error": f"HTTP_{resp.status_code}",
            "disclosureNo": disclosureNo,
        }

    ct = resp.headers.get("Content-Type", "")
    # PDF가 아니면서 본문이 짧으면 JSON 에러일 가능성
    if "pdf" not in ct.lower() and len(resp.content) < 2048:
        try:
            err = resp.json()
            return {"error": "API_ERROR", "detail": err}
        except Exception:
            pass

    Path(save_dir).mkdir(parents=True, exist_ok=True)
    fn = filename or f"alio_{disclosureNo}.pdf"
    save_path = os.path.join(save_dir, fn)
    with open(save_path, "wb") as f:
        f.write(resp.content)

    return {
        "saved_path": save_path,
        "size_bytes": len(resp.content),
        "content_type": ct,
    }


# ─────────────────────────────────────────────────────────────
# 서버 실행
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
