"""알리오 항목별공시 메뉴 조회 MCP 서버

한국 공공기관 정보공개시스템 알리오(www.alio.go.kr)의 항목별공시
83개 메뉴를 LLM 도구로 노출한다.

도구:
    list_menus(category) — 메뉴 목록 조회 (대분류 필터)
"""

from mcp.server.fastmcp import FastMCP
import requests

mcp = FastMCP("alio")

VALID_CATEGORIES = ["기관운영", "ESG 운영", "경영성과", "대내외 평가 등"]


def _normalize(s: str) -> str:
    return s.replace(" ", "").lower()


@mcp.tool()
def list_menus(category: str = "") -> list[dict]:
    """알리오 항목별공시 메뉴 목록을 반환한다.

    Args:
        category: 대분류명. '기관운영' / 'ESG 운영' / '경영성과' / '대내외 평가 등' 중 하나.
                  공백·대소문자 차이는 자동 흡수된다.
                  빈 문자열이면 전체 83개 메뉴를 반환한다.

    Returns:
        메뉴 리스트. 각 항목은 다음 필드를 포함한다.
            - 대분류: 4개 카테고리 중 하나
            - 항목명: 메뉴 이름 (예: '임직원 수')
            - rootNo: 알리오 내부 메뉴 식별자 (콤마 구분 다중 가능)
            - 보고서형: True=정형 보고서, False=게시판형
        매칭되는 카테고리가 없으면 NOT_FOUND 시그널과 유효한 대분류 목록을 반환한다.
    """
    resp = requests.post(
        "https://www.alio.go.kr/item/formList.json",
        json={},
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=10,
    )
    data = resp.json()["data"]

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


if __name__ == "__main__":
    mcp.run()
