"""알리오 코어 모듈 (alio-crawler + alio-mcp 공유 라이브러리)

알리오(www.alio.go.kr) 항목별공시 API 호출·파일 다운로드·HTML 파싱의
순수 함수 모음. tkinter/openpyxl 등 GUI/엑셀 의존 없음.

alio-crawler는 GUI 진입점에서 이 모듈을 import해 다운로드 로직을 위임하고,
alio-mcp는 도구(tool) 구현에 이 모듈의 함수를 그대로 사용한다.

추출 출처: alio_crawler_v5.4.py 라인 240~993 (v5.4.2 회귀 검증 통과).
"""

import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter


# ─────────────────────────────────────────────────────────
# 알리오 사이트 상수
# ─────────────────────────────────────────────────────────

BASE_URL = "https://www.alio.go.kr"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.alio.go.kr",
    "Referer": "https://www.alio.go.kr/item/itemList.do",
}

# 기관유형 (5개) - 2025년 1월 기준
INST_TYPE_CODES = {
    "전체": "",
    "공기업(시장형)": "공기업(시장형)",
    "공기업(준시장형)": "공기업(준시장형)",
    "준정부기관(기금관리형)": "준정부기관(기금관리형)",
    "준정부기관(위탁집행형)": "준정부기관(위탁집행형)",
    "기타공공기관": "기타공공기관",
}

# 주무부처 - ALIO 기준 (2025년 정부조직 개편 반영)
DEPT_CODES = {
    "전체": "",
    "경찰청": "경찰청",
    "고용노동부": "고용노동부",
    "공정거래위원회": "공정거래위원회",
    "과학기술정보통신부": "과학기술정보통신부",
    "관세청": "관세청",
    "교육부": "교육부",
    "국가데이터처": "국가데이터처",
    "국가보훈부": "국가보훈부",
    "국가유산청": "국가유산청",
    "국무조정실": "국무조정실",
    "국방부": "국방부",
    "국토교통부": "국토교통부",
    "금융위원회": "금융위원회",
    "기상청": "기상청",
    "기후에너지환경부": "기후에너지환경부",
    "기획예산처": "기획예산처",
    "농림축산식품부": "농림축산식품부",
    "농촌진흥청": "농촌진흥청",
    "문화체육관광부": "문화체육관광부",
    "방송미디어통신위원회": "방송미디어통신위원회",
    "방위사업청": "방위사업청",
    "법무부": "법무부",
    "보건복지부": "보건복지부",
    "산림청": "산림청",
    "산업통상부": "산업통상부",
    "성평등가족부": "성평등가족부",
    "소방청": "소방청",
    "식품의약품안전처": "식품의약품안전처",
    "외교부": "외교부",
    "원자력안전위원회": "원자력안전위원회",
    "인사혁신처": "인사혁신처",
    "재외동포청": "재외동포청",
    "재정경제부": "재정경제부",
    "중소벤처기업부": "중소벤처기업부",
    "지식재산처": "지식재산처",
    "통일부": "통일부",
    "해양수산부": "해양수산부",
    "행정안전부": "행정안전부",
}

# 지역 (17개)
REGION_CODES = {
    "전체": "",
    "강원도": "강원도",
    "경기도": "경기도",
    "경상남도": "경상남도",
    "경상북도": "경상북도",
    "광주광역시": "광주광역시",
    "대구광역시": "대구광역시",
    "대전광역시": "대전광역시",
    "부산광역시": "부산광역시",
    "서울특별시": "서울특별시",
    "세종특별자치시": "세종특별자치시",
    "울산광역시": "울산광역시",
    "인천광역시": "인천광역시",
    "전라남도": "전라남도",
    "전라북도": "전라북도",
    "제주특별자치도": "제주특별자치도",
    "충청남도": "충청남도",
    "충청북도": "충청북도",
}

# 공시 항목 목록 (v5.4 레거시. 코어에서는 fetch_alio_items 사용 권장)
DISCLOSURE_ITEMS = {
    "일반현황": {"rootNo": "10105", "type": "general"},
    "복리후생비": {"rootNo": "20801", "type": "jung"},
    "수의계약": {"rootNo": "70301,70302,70303,70304", "type": "jung"},
    "자체감사 결과(최근 5년)": {"rootNo": "43006", "type": "audit"},
    "징계제도 운영현황": {"rootNo": "2120", "type": "jung"},
    "징계처분 현황(최근 5년)": {"rootNo": "21214,21211,21212,21213", "type": "discipline"},
    "청렴도 평가 결과(최근 5년)": {"rootNo": "40211", "type": "integrity"},
    "사망자수(최근 5년)": {"rootNo": "70401", "type": "safety"},
    "환경법규 위반현황": {"rootNo": "B1270", "type": "envlaw"},
    "내부규정": {"rootNo": "", "type": "rule"},
    "경영실적 평가결과[공기업,준정부]": {"rootNo": "B1230", "type": "mgmt_eval"},
}

# 징계 종류 매핑
DISCIPLINE_TYPES = ["파면", "해임", "정직", "감봉", "견책", "기타"]

# 내부규정 분류 코드
RULE_DIVIS_CODES = {
    "전체": "",
    "정관": "K1500",
    "인사·복무·징계": "K1100",
    "보수": "K1200",
    "직제": "K1300",
    "기타": "K1400",
}

# 첨부파일 다운로드 엔드포인트 통합 레지스트리
ENDPOINT_REGISTRY = {
    "pdf":   "/download/pdf.json",          # 공시 PDF (disclosureNo 필요)
    "file":  "/download/file.json",         # 일반 첨부 (fileId, disclosureNo)
    "dfile": "/download/dfile.json",        # 안전경영책임보고서 (fileName, submissionNo)
    "rule":  "/download/rulefiledown.json", # 내부규정 (fileNo)
}


# ─────────────────────────────────────────────────────────
# 파일명 정제
# ─────────────────────────────────────────────────────────

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN = re.compile(r'\s+')


def sanitize_filename(name: str, max_len: int = 80) -> str:
    if not name:
        return "untitled"
    name = _INVALID_CHARS.sub("", name)
    name = _WHITESPACE_RUN.sub(" ", name).strip()
    name = name.strip(". ")
    if not name:
        return "untitled"
    base, ext = os.path.splitext(name)
    remaining = max_len - len(ext)
    if remaining < 1:
        remaining = 1
    base = base[:remaining]
    return base + ext


# ─────────────────────────────────────────────────────────
# 네트워크 유틸리티
# ─────────────────────────────────────────────────────────

_DEFAULT_TIMEOUT = (5, 30)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRIABLE_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
)


def create_session(
    verify_ssl: bool = True,
    timeout: tuple = _DEFAULT_TIMEOUT,
    max_pool_size: int = 10,
    user_agent: Optional[str] = None,
) -> requests.Session:
    session = requests.Session()
    session.verify = verify_ssl
    session.headers.update({
        "User-Agent": user_agent or _DEFAULT_USER_AGENT,
    })
    adapter = HTTPAdapter(
        pool_connections=max_pool_size,
        pool_maxsize=max_pool_size,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session._default_timeout = timeout
    return session


def retry_request(
    session: requests.Session,
    method: str,
    url: str,
    max_retries: int = 3,
    backoff: float = 1.0,
    **kwargs,
) -> requests.Response:
    if "timeout" not in kwargs:
        kwargs["timeout"] = getattr(session, "_default_timeout", _DEFAULT_TIMEOUT)
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code not in _RETRIABLE_STATUS_CODES:
                return response
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = backoff * (2 ** attempt)
                else:
                    wait = backoff * (2 ** attempt)
            else:
                wait = backoff * (2 ** attempt)
            if attempt < max_retries:
                jitter = random.uniform(0, wait * 0.5)
                time.sleep(wait + jitter)
                continue
            return response
        except _RETRIABLE_EXCEPTIONS as e:
            last_exception = e
            if attempt < max_retries:
                wait = backoff * (2 ** attempt)
                jitter = random.uniform(0, wait * 0.5)
                time.sleep(wait + jitter)
            else:
                raise


# ─────────────────────────────────────────────────────────
# 데이터 파싱 유틸
# ─────────────────────────────────────────────────────────

def parse_files_field(files_str):
    """
    files 필드 파싱: "101@파일명.pdf|102@파일명2.pdf" 형식
    반환: [{"id": "101", "name": "파일명.pdf"}, ...]
    """
    if not files_str:
        return []

    result = []
    parts = files_str.split('|')
    for part in parts:
        part = part.strip()
        if '@' in part:
            file_id, file_name = part.split('@', 1)
            result.append({
                "id": file_id.strip(),
                "name": file_name.strip(),
            })
        elif part:
            result.append({
                "id": "",
                "name": part,
            })
    return result


# ─────────────────────────────────────────────────────────
# 알리오 공시항목 자동 수집
# ─────────────────────────────────────────────────────────

def fetch_alio_items(progress_callback=None):
    """알리오 항목별공시 전체 메뉴를 formList.json에서 가져온다.

    응답 항목 구조 예:
        {"lcd": "2", "lcdnm": "기관운영", "nmcd": "RPTMGR0201",
         "nmcdnm": "일반현황", "mcd": "10105", "mcdnm": "일반현황",
         "reportYn": "Y", "reportType": "4", "reportNos": "10105", ...}

    반환: 항목 리스트 (실패 시 빈 리스트)
    """
    url = f"{BASE_URL}/item/formList.json"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/item/itemList.do",
    }

    if progress_callback:
        progress_callback(0, 1, "알리오 항목 메뉴 조회 중...")

    sess = create_session(verify_ssl=True)
    try:
        resp = retry_request(sess, "POST", url, json={}, headers=headers, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("status") != "success":
            return []
        items = data.get("data", []) or []
        if progress_callback:
            progress_callback(1, 1, f"{len(items)}개 항목 수집 완료")
        return items
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"항목 메뉴 조회 실패: {e}")
        return []


def get_alio_items_cache_path(cache_dir: Optional[str] = None) -> str:
    """캐시 파일 경로. cache_dir 미지정 시 alio_core.py가 있는 폴더."""
    if cache_dir is None:
        cache_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(cache_dir, "alio_items.json")


def load_alio_items_cache(cache_dir: Optional[str] = None):
    """캐시 로드 — 없거나 손상되면 None"""
    cache_path = get_alio_items_cache_path(cache_dir)
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"항목 캐시 로드 실패: {e}")
        return None


def save_alio_items_cache(items, cache_dir: Optional[str] = None) -> bool:
    """항목 메뉴를 캐시 파일에 저장"""
    cache_path = get_alio_items_cache_path(cache_dir)
    cache_data = {
        "scanned_at": datetime.now().isoformat(),
        "total_count": len(items),
        "items": items,
    }
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"항목 캐시 저장 실패: {e}")
        return False


def get_alio_items(force_refresh: bool = False, progress_callback=None,
                   cache_dir: Optional[str] = None):
    """
    캐시 우선, 없거나 force_refresh=True면 API 재조회.
    반환: 항목 리스트
    """
    if not force_refresh:
        cached = load_alio_items_cache(cache_dir)
        if cached and cached.get("items"):
            return cached["items"]

    items = fetch_alio_items(progress_callback=progress_callback)
    if items:
        save_alio_items_cache(items, cache_dir)
    return items


def build_item_display_name(item):
    """항목 dict → 사용자에게 보여줄 항목명. scdnm이 있으면 우선."""
    scdnm = (item.get("scdnm") or "").strip()
    mcdnm = (item.get("mcdnm") or "").strip()
    return scdnm or mcdnm or item.get("mcd", "(미상)")


def build_item_root_no(item):
    """항목 dict → API 호출에 쓸 rootNo (콤마 다중 가능)."""
    return (item.get("reportNos") or item.get("mcd") or "").strip()


def group_items_by_category(items):
    """항목 리스트 → {대분류: {중분류: [item, ...]}} 트리"""
    tree = {}
    for it in items:
        lcdnm = it.get("lcdnm") or "기타"
        nmcdnm = it.get("nmcdnm") or "기타"
        tree.setdefault(lcdnm, {}).setdefault(nmcdnm, []).append(it)
    return tree


# ─────────────────────────────────────────────────────────
# 첨부파일 다운로드 (보고서형 통합)
# ─────────────────────────────────────────────────────────

def detect_endpoint_kind(item_meta):
    """formList.json 응답 메타로 다운로드 엔드포인트 종류 추정.

    - 내부규정(mcd=21110) → "rule"
    - 사망자수+안전경영책임보고서(reportNos에 70401) → "pdf+file+dfile"
    - 보고서형(reportYn=Y) → "pdf+file"
    - 게시판형(reportYn=N) → "file"
    """
    mcd = item_meta.get("mcd", "") or ""
    if mcd == "21110":
        return "rule"

    report_nos = item_meta.get("reportNos", "") or ""
    if "70401" in report_nos:
        return "pdf+file+dfile"

    if (item_meta.get("reportYn", "") or "").upper() == "Y":
        return "pdf+file"
    return "file"


def build_save_path(base_dir, item_name, inst_name):
    """저장 폴더 구조: base_dir / 항목명 / 기관명 (자동 생성)"""
    item_safe = sanitize_filename(item_name, max_len=50)
    inst_safe = sanitize_filename(inst_name, max_len=50)
    full_path = os.path.join(base_dir, item_safe, inst_safe)
    os.makedirs(full_path, exist_ok=True)
    return full_path


def _resolve_collision_path(target_path):
    """동일 파일명 충돌 시 (1), (2), ... 부여."""
    if not os.path.exists(target_path):
        return target_path
    base, ext = os.path.splitext(target_path)
    n = 1
    while os.path.exists(f"{base}({n}){ext}"):
        n += 1
    return f"{base}({n}){ext}"


def download_file_to_path(session, url, save_path, params=None, timeout=60,
                          max_retries=3, backoff=1.0):
    """파일 단일 다운로드. JSON 응답이면 API 에러로 간주.

    스트리밍(iter_content) 도중 연결이 끊기는 경우(ConnectionResetError·ReadTimeout
    등)는 retry_request로 잡히지 않는다 — 응답 헤더(200) 수신 후 본문 전송 중
    발생하기 때문이다. 따라서 요청+스트리밍 전체를 max_retries만큼 재시도하고,
    부분 파일이 남지 않도록 .part 임시파일에 받은 뒤 성공 시 원자적으로 교체한다.

    반환: (success, saved_path, message)
    """
    target = _resolve_collision_path(save_path)
    tmp = target + ".part"
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            # 재시도 책임을 이 외부 루프로 일원화한다(max_retries=0). 그러지 않으면
            # retry_request 내부 재시도(기본 3회)와 외부 루프(max_retries회)가 곱해져
            # 헤더 단계 오류 시 최대 (n+1)^2회·backoff 누적으로 장시간 블로킹된다.
            # 본문 스트리밍 끊김은 어차피 retry_request가 못 잡으므로 외부 루프가 필요.
            resp = retry_request(session, "GET", url, params=params, timeout=timeout,
                                 stream=True, max_retries=0)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code in _RETRIABLE_STATUS_CODES and attempt < max_retries:
                    time.sleep(backoff * (2 ** attempt) + random.uniform(0, 0.5))
                    continue
                return False, "", last_err

            ct = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ct:
                try:
                    err = resp.json()
                    return False, "", f"API error: {err.get('message') or 'unknown'}"
                except (json.JSONDecodeError, ValueError):
                    pass

            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, target)
            return True, target, "OK"
        except (requests.RequestException, OSError) as e:
            last_err = str(e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if attempt < max_retries:
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, 0.5))
                continue
            return False, "", last_err
    return False, "", last_err


# ─────────────────────────────────────────────────────────
# 게시판형 첨부파일 (HTML 파싱)
# ─────────────────────────────────────────────────────────

def fetch_board_attachment_list(session, apba_id, violation_meta):
    """게시판형 자료의 첨부파일 메타를 itemBoard{reportFormNo}.do HTML에서 추출.

    두 가지 패턴 통합:
    - 패턴 A: ``downAttachFile('spath', 'sfile', 'dfile')`` → ``/upload{spath}{sfile}``
              (예: B1220 감사원 지적사항, B1210 국회지적사항)
    - 패턴 B: ``<a href="/download/download.json?fileNo=N">파일명</a>``
              (예: B1010 임원 모집공고, B1020 직원 채용정보)

    반환: [{"kind": "upload"|"fileno", ...}]
    """
    rfn = (violation_meta.get("report_form_no") or "").strip()
    if not rfn:
        return []

    params = {
        "disclosureNo": violation_meta.get("disclosure_no") or "",
        "apbaId": apba_id,
        "nowcode": rfn,
        "reportFormNo": rfn,
        "table_name": violation_meta.get("table_name") or "",
        "idx_name": violation_meta.get("idx_name") or "",
        "idx": violation_meta.get("idx") or "",
        "reportGbn": "N",
        "bid_type": violation_meta.get("bid_type") or "",
    }
    url = f"{BASE_URL}/item/itemBoard{rfn}.do"

    try:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        text = resp.text
        attachments = []
        seen = set()

        # 패턴 A: downAttachFile('spath', 'sfile', 'dfile')
        pat_a = r"downAttachFile\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
        for m in re.finditer(pat_a, text):
            spath, sfile, dfile = m.group(1), m.group(2), m.group(3)
            key = ("upload", spath, sfile)
            if key in seen:
                continue
            seen.add(key)
            attachments.append({
                "kind": "upload",
                "spath": spath, "sfile": sfile, "dfile": dfile,
            })

        # 패턴 B: <a href="/download/download.json?fileNo=N">파일명</a>
        pat_b = r'<a[^>]*\bhref=["\'][^"\']*?/download/download\.json\?fileNo=(\d+)["\'][^>]*>([^<]+)</a>'
        for m in re.finditer(pat_b, text):
            file_no = m.group(1)
            file_name = m.group(2).strip()
            key = ("fileno", file_no)
            if key in seen:
                continue
            seen.add(key)
            attachments.append({
                "kind": "fileno",
                "file_no": file_no, "dfile": file_name,
            })

        return attachments
    except (requests.RequestException, OSError):
        return []


def fetch_board_external_links(session, apba_id, violation_meta):
    """게시판형 자료의 외부 링크 추출 (입찰공고 g2b.go.kr 등).

    알리오 일부 게시판형은 첨부 대신 외부 사이트 링크만 제공.
    반환: [{"url": "https://...", "text": "링크 텍스트"}, ...]
    """
    rfn = (violation_meta.get("report_form_no") or "").strip()
    if not rfn:
        return []

    params = {
        "disclosureNo": violation_meta.get("disclosure_no") or "",
        "apbaId": apba_id,
        "nowcode": rfn, "reportFormNo": rfn,
        "table_name": violation_meta.get("table_name") or "",
        "idx_name": violation_meta.get("idx_name") or "",
        "idx": violation_meta.get("idx") or "",
        "reportGbn": "N",
        "bid_type": violation_meta.get("bid_type") or "",
    }
    url = f"{BASE_URL}/item/itemBoard{rfn}.do"

    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return []
        text = resp.text
        pat = r'<a[^>]*\bhref=["\'](https?://[^"\']+)["\'][^>]*>([^<]*)</a>'
        seen = set()
        external = []
        for m in re.finditer(pat, text):
            link = m.group(1).replace("&amp;", "&")
            if "alio.go.kr" in link or link in seen:
                continue
            seen.add(link)
            external.append({"url": link, "text": m.group(2).strip()[:80]})
        return external
    except (requests.RequestException, OSError):
        return []


def download_board_attachment(session, attachment, save_dir):
    """게시판형 첨부파일 1건 다운로드 (두 패턴 통합).

    kind="upload": ``/upload{spath}{sfile}`` 직접 GET
    kind="fileno": ``/download/download.json?fileNo=N`` GET
    """
    kind = (attachment.get("kind") or "upload").strip()
    dfile = (attachment.get("dfile") or "").strip()

    if kind == "upload":
        spath = (attachment.get("spath") or "").strip()
        sfile = (attachment.get("sfile") or "").strip()
        if not spath or not sfile:
            return False, "", "missing spath/sfile"
        if not spath.startswith("/"):
            spath = "/" + spath
        url = f"{BASE_URL}/upload{spath}{sfile}"
        save_path = os.path.join(save_dir, sanitize_filename(dfile or sfile, max_len=120))
        return download_file_to_path(session, url, save_path)

    if kind == "fileno":
        file_no = (attachment.get("file_no") or "").strip()
        if not file_no:
            return False, "", "missing file_no"
        url = f"{BASE_URL}/download/download.json?fileNo={file_no}"
        save_path = os.path.join(save_dir, sanitize_filename(dfile or f"file_{file_no}", max_len=120))
        return download_file_to_path(session, url, save_path)

    return False, "", f"unknown kind: {kind}"


def download_attachment(session, kind, file_info, save_dir,
                        disclosure_no="", submission_no=""):
    """통합 첨부파일 다운로드 함수.

    kind:
        - "pdf"   : disclosureNo로 공시 PDF 받기
        - "file"  : fileId + disclosureNo로 첨부 받기
        - "dfile" : fileName + submissionNo로 안전경영책임보고서 받기
        - "rule"  : fileNo로 내부규정 파일 받기

    file_info: {"id": "...", "name": "...."}
    반환: (success, saved_path, message)
    """
    base = ENDPOINT_REGISTRY.get(kind)
    if not base:
        return False, "", f"unknown kind: {kind}"

    url = f"{BASE_URL}{base}"
    file_name = file_info.get("name", "untitled")
    save_path = os.path.join(save_dir, sanitize_filename(file_name, max_len=120))

    if kind == "pdf":
        params = {"disclosureNo": disclosure_no}
    elif kind == "file":
        params = {"f": file_info.get("id", ""), "d": disclosure_no}
    elif kind == "dfile":
        params = {"fileName": file_name, "submissionNo": submission_no}
    elif kind == "rule":
        params = {"fileNo": file_info.get("id", "")}
    else:
        return False, "", f"unsupported kind: {kind}"

    return download_file_to_path(session, url, save_path, params=params)


# ─────────────────────────────────────────────────────────
# 공공기관 목록 (지역 포함, 병렬 처리)
# ─────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────
# 보고서형 본문(HTML 표) 조회 — itemReportRight.do
# ─────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """HTML → 평문 (script/style 제거·태그 제거·엔티티 복원·공백 정규화)."""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def parse_html_tables(html: str) -> list:
    """HTML <table>들을 행렬(list[list[str]])로 추출. 빈 행·빈 표 제외.

    itemReportRight.do는 Vue 레이아웃 table이 섞여 있어 비탐욕 매칭으로
    가장 안쪽 셀까지 평문화한다. 표 경계가 부정확할 수 있으므로 본문
    평문(html_to_text)을 항상 함께 제공해 보조한다.
    """
    tables = []
    for tm in re.finditer(r"(?is)<table[^>]*>(.*?)</table>", html):
        rows = []
        for rm in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", tm.group(1)):
            cells = [
                html_to_text(c)
                for c in re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", rm.group(1))
            ]
            if any(cell for cell in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def fetch_report_tables(session, disclosure_no: str) -> dict:
    """보고서형 공시의 본문을 itemReportRight.do HTML에서 표·평문으로 추출.

    download_report(PDF 저장)와 달리 파일을 만들지 않고 내용을 즉시 반환한다.
    징계현황·임직원수·복리후생비 등 보고서형 항목의 실데이터를 PDF/HWP 변환
    없이 LLM이 바로 읽을 수 있다. pdf.json이 "해당 사항이 없는 항목입니다"
    boilerplate를 주는 항목(임직원수·임원연봉 등)도 이 HTML 경로는 실데이터다.

    Args:
        session: create_session() 세션.
        disclosure_no: 공시번호 (list_organs / itemOrganListJung의 disclosureNo).

    반환:
        성공: {"disclosureNo", "제목", "표_개수", "표": [[셀,...],...], "본문텍스트"}
        실패: {"error": "..."}
    """
    if not disclosure_no:
        return {"error": "MISSING: disclosureNo가 필수입니다"}

    url = f"{BASE_URL}/item/itemReportRight.do"
    try:
        resp = retry_request(session, "GET", url,
                             params={"disclosureNo": disclosure_no})
        if resp.status_code != 200:
            return {"error": f"REQUEST_FAILED: HTTP {resp.status_code}"}
    except (requests.RequestException, OSError) as e:
        return {"error": f"REQUEST_FAILED: {e}"}

    html = resp.text
    tables = parse_html_tables(html)
    text = html_to_text(html)
    if not tables and not text:
        return {"error": f"EMPTY: disclosureNo='{disclosure_no}' 본문 없음 "
                          "(순수 첨부 항목일 수 있음 — download_* 도구 사용)"}

    return {
        "disclosureNo": disclosure_no,
        "제목": text[:60].strip(),
        "표_개수": len(tables),
        "표": tables,
        "본문텍스트": text[:4000],
    }


def fetch_disclosure_attachments(session, disclosure_no: str) -> dict:
    """보고서형 공시의 부속 첨부 메타를 itemReportFiles.json에서 조회.

    download_disclosure_attachment 호출에 필요한 식별자를 한 번에 제공한다:
      - fileNo       → kind='file'의 fileId (file.json ?f=fileNo&d=disclosureNo)
      - fileName     → kind='dfile'의 fileName (dfile.json ?fileName=&submissionNo=)
      - submissionNo → kind='dfile'에 필요

    itemOrganListJung의 'files'(id@name) 필드와 동일 파일집합을 가리키되, 보고서형
    전반(손익계산서·복리후생비 부속·안전경영책임보고서 등)에 공통이고 disclosureNo
    하나로 조회되므로 도구 노출에 적합하다.

    반환: {"disclosureNo", "첨부": [{"fileNo","fileName","submissionNo",
           "fileType","savePath"}, ...]} | {"error": ...}
    """
    if not disclosure_no:
        return {"error": "MISSING: disclosureNo가 필수입니다"}
    url = f"{BASE_URL}/item/itemReportFiles.json"
    try:
        resp = retry_request(session, "GET", url,
                             params={"disclosureNo": disclosure_no}, timeout=20)
        if resp.status_code != 200:
            return {"error": f"REQUEST_FAILED: HTTP {resp.status_code}"}
        raw = resp.json().get("data", [])
        data = raw if isinstance(raw, list) else []  # 스칼라/딕트 응답에도 안전(Node Array.isArray 가드와 동등)
    except (requests.RequestException, ValueError, OSError) as e:
        return {"error": f"REQUEST_FAILED: {e}"}
    files = []
    for f in data:
        if not isinstance(f, dict):
            continue
        files.append({
            "fileNo": str(f.get("fileNo", "") or ""),
            "fileName": f.get("orcpFileNa", "") or f.get("saveFileNa", "") or "",
            "submissionNo": str(f.get("submissionNo", "") or ""),
            "fileType": f.get("fileType", "") or "",
            "savePath": f.get("savePath", "") or "",
        })
    return {"disclosureNo": disclosure_no, "첨부": files}


def load_public_institutions(progress_callback=None):
    """ALIO 기관목록 API에서 기관 목록 로드 (지역 정보 포함).

    반환: {기관명: {"apba_id", "inst_type", "dept", "region"}, ...}
    """
    try:
        url = f"{BASE_URL}/organ/findOrganApbaList.json"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        body = {
            "apbaType": [],
            "jidtDptm": [],
            "area": [],
            "apba_id": "",
            "pageNo": 1,
        }

        _sess = create_session(verify_ssl=True)
        resp = retry_request(_sess, "POST", url, json=body, headers=headers, timeout=30)
        if resp.status_code != 200:
            return {}

        data = resp.json()
        page_info = data.get('data', {}).get('organList', {}).get('page', {})
        total_page = page_info.get('totalPage', 1)

        inst_dict = {}

        organ_list = data.get('data', {}).get('organList', {}).get('result', [])
        for item in organ_list:
            name = item.get("apbaNa", "")
            if name:
                inst_dict[name] = {
                    "apba_id": item.get("apbaId", ""),
                    "inst_type": item.get("typeNa", ""),
                    "dept": item.get("jidtNa", ""),
                    "region": item.get("addrCd", "") or "",
                }

        if progress_callback:
            progress_callback(1, total_page)

        if total_page <= 1:
            return inst_dict

        def fetch_page(page_no):
            try:
                body = {
                    "apbaType": [],
                    "jidtDptm": [],
                    "area": [],
                    "apba_id": "",
                    "pageNo": page_no,
                }
                resp = retry_request(_sess, "POST", url, json=body, headers=headers, timeout=30)
                if resp.status_code == 200:
                    return resp.json().get('data', {}).get('organList', {}).get('result', [])
            except requests.RequestException:
                pass
            return []

        completed = 1
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_page, p): p for p in range(2, total_page + 1)}
            for future in as_completed(futures):
                organ_list = future.result()
                for item in organ_list:
                    name = item.get("apbaNa", "")
                    if name:
                        inst_dict[name] = {
                            "apba_id": item.get("apbaId", ""),
                            "inst_type": item.get("typeNa", ""),
                            "dept": item.get("jidtNa", ""),
                            "region": item.get("addrCd", "") or "",
                        }
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_page)

        return inst_dict
    except Exception as e:
        print(f"공공기관 목록 로드 실패: {e}")
        return {}


# ─────────────────────────────────────────────────────────
# 기관 프로필 / 다중기관 disclosureNo / 정형 집계 (v1.3.0)
# ─────────────────────────────────────────────────────────

def _clean_field(v) -> str:
    """API 값 정규화 — None/'None'/'null'은 빈 문자열로."""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("none", "null") else s


def fetch_organ_profile(session, apba_id: str) -> dict:
    """findOrganApbaList.json의 apba_id 필터로 기관 마스터 정보 1건 조회.

    search_organs가 주지 않는 기관장·홈페이지·주소·설립일·소개 등을 반환한다.
    반환: {기관ID, 기관명, 기관유형, 주무부처, 기관장, 홈페이지, 주소, 지역,
           설립일, 예산, 소개, 유튜브, 상위기관, submissionNo} | {"error": ...}
    """
    if not apba_id:
        return {"error": "MISSING: apba_id가 필수입니다"}
    url = f"{BASE_URL}/organ/findOrganApbaList.json"
    headers = {"Content-Type": "application/json;charset=UTF-8",
               "X-Requested-With": "XMLHttpRequest"}
    body = {"apbaType": [], "jidtDptm": [], "area": [], "apba_id": apba_id, "pageNo": 1}
    try:
        resp = retry_request(session, "POST", url, json=body, headers=headers, timeout=30)
        if resp.status_code != 200:
            return {"error": f"REQUEST_FAILED: HTTP {resp.status_code}"}
        node = resp.json().get("data", {}).get("organList", {})
        result = node.get("result", []) if isinstance(node, dict) else (node or [])
    except (requests.RequestException, OSError, ValueError) as e:
        return {"error": f"REQUEST_FAILED: {e}"}
    match = next((o for o in result if o.get("apbaId") == apba_id), None)
    if not match:
        return {"error": f"NOT_FOUND: apba_id='{apba_id}' 기관 없음"}
    return {
        "기관ID": match.get("apbaId", ""),
        "기관명": match.get("apbaNa", ""),
        "기관유형": _clean_field(match.get("typeNa")),
        "주무부처": _clean_field(match.get("jidtNa")),
        "기관장": _clean_field(match.get("ceo")),
        "홈페이지": _clean_field(match.get("homepage")),
        "주소": _clean_field(match.get("addr1")),
        "지역": _clean_field(match.get("addrCd")),
        "설립일": _clean_field(match.get("fdate")),
        "예산": _clean_field(match.get("fmoney")),
        "소개": _clean_field(match.get("contents")).replace("&cr;", "\n"),
        "유튜브": _clean_field(match.get("youtUrl")),
        "상위기관": _clean_field(match.get("parnApbaNa")),
        "submissionNo": _clean_field(match.get("submissionNo")),
    }


def fetch_organ_disclosure_map(session, root_no: str, apba_ids) -> dict:
    """itemOrganListJung 1회 호출로 지정 기관들의 disclosureNo만 추출.

    itemOrganListJung은 apba_id 필터를 무시하므로 전체를 받아 클라이언트에서
    apba_ids만 추린다. 전체 organList는 반환하지 않는다(토큰 절약).
    반환: {apba_id: {"기관명", "disclosureNo", "submissionNo"}, ...}
    """
    primary = (root_no or "").split(",")[0].strip()
    if not primary:
        return {}
    want = set(apba_ids or [])
    url = f"{BASE_URL}/item/itemOrganListJung.json"
    body = {"reportFormRootNo": primary, "apbaType": [], "jidtDptm": [],
            "area": [], "apba_id": "", "pageNo": 1}
    try:
        resp = retry_request(session, "POST", url, json=body,
                             headers={"Content-Type": "application/json;charset=UTF-8"}, timeout=30)
        organs = (resp.json().get("data") or {}).get("organList", [])
        if isinstance(organs, dict):
            organs = organs.get("result", [])
    except (requests.RequestException, OSError, ValueError):
        return {}
    out = {}
    for o in (organs or []):
        aid = o.get("apbaId")
        if aid in want:
            out[aid] = {
                "기관명": o.get("apbaNa", ""),
                "disclosureNo": (o.get("disclosureNo") or "").strip(),
                "submissionNo": (o.get("submissionNo") or "").strip(),
            }
    return out


# 징계종류 표준 6종 (중징계: 파면·해임·강등·정직 / 경징계: 감봉·견책)
_DISCIPLINE_CATS = ["파면", "해임", "강등", "정직", "감봉", "견책"]
_YEAR_RE = re.compile(r"^(\d{4})년$")


def summarize_discipline_table(tables) -> dict:
    """징계처분 현황(21211) itemReportRight.do 표에서 징계종류별 건수 집계.

    '징계종류' 헤더를 가진 표를 찾아 해당 컬럼을 분류·카운트한다.
    출근정지→정직, 표준 6종 부분일치, 미매칭→기타.
    반환: {"징계건수": {종류: n}, "총건수": N, "기타종류": [원문, ...]}
    """
    counts = {c: 0 for c in _DISCIPLINE_CATS}
    counts["기타"] = 0
    others = []
    total = 0
    for tbl in (tables or []):
        if not tbl or "징계종류" not in tbl[0]:
            continue
        col = tbl[0].index("징계종류")
        for row in tbl[1:]:
            if col >= len(row):
                continue
            jong = (row[col] or "").strip()
            if not jong:
                continue
            total += 1
            if "출근정지" in jong:
                counts["정직"] += 1
                continue
            matched = next((c for c in _DISCIPLINE_CATS if c in jong), None)
            if matched:
                counts[matched] += 1
            else:
                counts["기타"] += 1
                others.append(jong)
        break  # 첫 '징계종류' 표만
    return {"징계건수": counts, "총건수": total, "기타종류": others}


def summarize_integrity_table(tables) -> dict:
    """청렴도 평가결과(40211) 표에서 연도별 등급 추출.

    헤더의 'YYYY년' 컬럼을 연도로, '청렴도' 포함 행의 연도별 값을 등급으로.
    반환: {"연도별등급": {"2021": "4등급", ...}, "연도": [...]}
    """
    for tbl in (tables or []):
        if not tbl:
            continue
        year_cols = []
        for i, h in enumerate(tbl[0]):
            m = _YEAR_RE.match((h or "").strip())
            if m:
                year_cols.append((i, m.group(1)))
        if not year_cols:
            continue
        grade_row = next((r for r in tbl[1:] if r and "청렴도" in (r[0] or "")), None)
        if not grade_row:
            continue
        grades = {}
        for i, yr in year_cols:
            val = (grade_row[i] or "").strip() if i < len(grade_row) else ""
            grades[yr] = val if (val and val != "해당없음") else "-"
        return {"연도별등급": grades, "연도": [yr for _, yr in year_cols]}
    return {"연도별등급": {}, "연도": []}


# ─────────────────────────────────────────────────────────
# 내부규정 (findRuleList → findRuleDtl → rulefiledown)
# ─────────────────────────────────────────────────────────

def fetch_rule_list(session, inst_name: str, divis: str = "", page: int = 1) -> dict:
    """기관명으로 내부규정 목록 1페이지 조회.

    Args:
        inst_name: 기관명 (예: '한국산업단지공단'). apbaNa로 검색.
        divis: 내부규정 분류 코드 (RULE_DIVIS_CODES). 빈 문자열이면 전체.
               'K1500'(정관)·'K1100'(인사·복무·징계)·'K1200'(보수)·
               'K1300'(직제)·'K1400'(기타).
        page: 페이지 번호 (1부터).

    Returns:
        {"totalCnt", "result": [{"seq", "title", "insdRuleDivis", ...}, ...]}
        실패 시 {"error", "result": []}.
    """
    url = f"{BASE_URL}/occasional/findRuleList.json"
    params = {
        "type": "apbaNa",
        "word": inst_name,
        "pageNo": page,
        "divis": divis,
    }
    try:
        resp = retry_request(session, "GET", url, params=params, timeout=30)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "result": []}
        data = resp.json().get("data") or {}
        return {
            "totalCnt": data.get("totalCnt", 0),
            "result": data.get("result", []) or [],
        }
    except (requests.RequestException, ValueError) as e:
        return {"error": str(e), "result": []}


def fetch_all_rules(session, inst_name: str, divis: str = "") -> list:
    """기관 내부규정 전체 페이지 자동 순회.

    Args:
        inst_name: 기관명.
        divis: 분류 코드.

    Returns:
        [{"seq", "title", "insdRuleDivis", ...}, ...] 전체 목록.
        오류·없음 시 빈 리스트.
    """
    first = fetch_rule_list(session, inst_name, divis, page=1)
    items = list(first.get("result", []))
    total_cnt = first.get("totalCnt", 0)
    if total_cnt <= len(items) or not items:
        return items
    # 알리오 findRuleList 페이지 사이즈는 통상 10건 — 전체 수 / 페이지 사이즈
    page_size = max(len(items), 1)
    total_page = (total_cnt + page_size - 1) // page_size
    for p in range(2, total_page + 1):
        more = fetch_rule_list(session, inst_name, divis, page=p)
        items.extend(more.get("result", []))
    return items


def fetch_rule_detail(session, seq: str) -> dict:
    """규정 상세 조회. bFiles에서 .zip 제외하고 fileNo가 가장 큰(최신) 파일 메타 반환.

    Args:
        seq: 규정 시퀀스 (fetch_rule_list 응답의 'seq').

    Returns:
        {"seq", "bFiles_raw", "files": [{"file_no", "file_name"}, ...], "latest": {...} | None}
        실패 시 {"error", ...}.
    """
    url = f"{BASE_URL}/occasional/findRuleDtl.json"
    try:
        resp = retry_request(session, "GET", url, params={"seq": seq}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "files": [], "latest": None}
        data = resp.json().get("data") or {}
        b_files = data.get("bFiles", "") or ""
        files = []
        for entry in b_files.split(","):
            entry = entry.strip()
            if "|" not in entry:
                continue
            file_no, file_name = entry.split("|", 1)
            file_no = file_no.strip()
            file_name = file_name.strip()
            if file_name.lower().endswith(".zip"):
                continue
            files.append({"file_no": file_no, "file_name": file_name})
        latest = None
        if files:
            try:
                latest = max(files, key=lambda f: int(f["file_no"]))
            except (ValueError, TypeError):
                latest = files[-1]
        return {
            "seq": seq,
            "bFiles_raw": b_files,
            "files": files,
            "latest": latest,
        }
    except (requests.RequestException, ValueError) as e:
        return {"error": str(e), "files": [], "latest": None}


def download_rule_file_to_path(session, file_no: str, save_path: str):
    """fileNo로 내부규정 파일 단건 다운로드. download_file_to_path 위임."""
    url = f"{BASE_URL}/download/rulefiledown.json"
    return download_file_to_path(session, url, save_path, params={"fileNo": file_no})


# ─────────────────────────────────────────────────────────
# 게시판형·보고서 자료 전체 페이지 자동 순회 (audit·mgmt_eval·감사원 등 통합 wrapper)
# ─────────────────────────────────────────────────────────

def fetch_all_board_items(session, root_no: str, apba_id: str = "",
                          apba_type: str = "") -> list:
    """itemReportListSusi.json을 페이지 자동 순회로 모든 자료 반환.

    audit(43006)·mgmt_eval(B1230)·감사원지적사항(B1220) 등 게시판형/보고서형
    모두 동일 엔드포인트라 한 함수로 처리한다.

    Args:
        root_no: 게시판/보고서 항목 식별자 (예: 'B1220', '43006').
        apba_id: 기관 ID. 빈 문자열이면 전체 기관.
        apba_type: 기관 유형 필터 (보통 빈 문자열).

    Returns:
        [{"제목", "등록일", "기관ID", "공시번호", "제출번호", "idx",
          "reportFormNo", "tableName", "idxName", "bidType"}, ...]
        실패 시 빈 리스트.
    """
    url = f"{BASE_URL}/item/itemReportListSusi.json"
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    items = []
    page_no = 1
    total_page = 1

    while page_no <= total_page:
        body = {
            "pageNo": page_no,
            "apbaId": apba_id,
            "apbaType": apba_type,
            "reportFormRootNo": root_no,
            "search_word": "",
            "search_flag": "title",
            "bid_type": "",
            "enfc_istt": "",
        }
        try:
            resp = retry_request(session, "POST", url, json=body, headers=headers, timeout=30)
            if resp.status_code != 200:
                break
            data = resp.json().get("data") or {}
            page_info = data.get("page", {}) or {}
            total_page = page_info.get("totalPage", 1) or 1
            for v in (data.get("result", []) or []):
                items.append({
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
                })
        except (requests.RequestException, ValueError):
            break
        page_no += 1

    return items
