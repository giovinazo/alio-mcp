#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""알리오 전체 공공기관 임원현황 + 내부규정(정관·직제규정·직제규정시행세칙) 일괄 수집.

- 대상: 알리오 항목별공시 rootNo 20305(임원현황) 공시 기관 전체(약 355개)
- 산출: NAS 기관별 폴더에 임원현황.pdf / 정관 / 직제규정 / 직제규정시행세칙
- 부수 산출: _임원현황_통합.xlsx, _수집매니페스트.csv, _미매칭_규정_리포트.csv, _기관로스터.csv
- 규정 매칭은 findRuleList의 apbaId로 정확 필터(교차오염 방지) + 기관명 부분문자열 폴백.
- 멱등: 임원현황 PDF는 존재 시 건너뜀. 규정은 --overwrite-rules로 재수집 가능.

사용:
  python3 collect_exec_and_rules.py                      # 전체(임원현황+규정)
  python3 collect_exec_and_rules.py --overwrite-rules    # 규정 매칭 개선 후 규정 재수집
  python3 collect_exec_and_rules.py --limit 5
  python3 collect_exec_and_rules.py --ids C0208,C0005
"""
import sys
import os
import re
import csv
import glob
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ALIO_MCP = os.path.dirname(HERE)
sys.path.insert(0, ALIO_MCP)
import alio_core as ac                       # noqa: E402

# ─────────────────────────────────────────────────────────
# 출력 루트: 기본은 이 스크립트 옆 output/, 환경변수 ALIO_OUT_ROOT로 재정의 가능
OUT_ROOT = os.environ.get(
    "ALIO_OUT_ROOT",
    os.path.join(HERE, "output", "공공기관_임원·규정_20260602"),
)
ROOT_NO = "20305"
MIN_PDF = 1500
MIN_RULE = 1500

EXEC_COLS = ["기관명", "기관유형", "주무부처", "직위", "직책", "성명", "성별",
             "임기시작", "임기종료", "당연직여부", "임명권자",
             "선임절차", "선임근거규정", "주요경력", "공시기준일", "공시번호"]
MANIFEST_COLS = ["기관ID", "기관명", "기관유형", "주무부처",
                 "임원현황", "임원수", "공시기준일", "공시번호",
                 "정관", "직제규정", "직제규정시행세칙", "규정검색어"]
MISS_COLS = ["기관ID", "기관명", "기관유형", "항목", "상태",
             "정관_후보", "직제_후보"]

_tl = threading.local()


def sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = ac.create_session()
        _tl.s = s
    return s


# ─────────────────────────────────────────────────────────
# 조회
# ─────────────────────────────────────────────────────────
def get_roster(s):
    body = {"reportFormRootNo": ROOT_NO, "apbaType": [], "jidtDptm": [],
            "area": [], "apba_id": "", "pageNo": 1}
    r = ac.retry_request(s, "POST", f"{ac.BASE_URL}/item/itemOrganListJung.json",
                         json=body,
                         headers={"Content-Type": "application/json;charset=UTF-8"},
                         timeout=30)
    organs = (r.json().get("data") or {}).get("organList", []) or []
    return [{"id": o.get("apbaId"), "name": o.get("apbaNa"),
             "type": o.get("typeNa"), "dept": o.get("jidtNa")} for o in organs]


def latest_disclosure(s, apba_id):
    body = {"pageNo": 1, "apbaId": apba_id, "apbaType": "",
            "reportFormRootNo": ROOT_NO, "search_word": "",
            "search_flag": "title", "bid_type": "", "enfc_istt": ""}
    try:
        r = ac.retry_request(
            s, "POST", f"{ac.BASE_URL}/item/itemReportListSusi.json",
            json=body, headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=20)
        res = (r.json().get("data") or {}).get("result", []) or []
        if res:
            return res[0].get("disclosureNo"), res[0].get("idate")
    except Exception:
        pass
    return None, None


def _search_terms(name):
    """기관명 → findRuleList 검색어 후보(정식명 → 구분자 제거 → 토큰 길이순)."""
    seen = []
    def add(t):
        t = (t or "").strip()
        if t and len(t) >= 2 and t not in seen:
            seen.append(t)
    add(name)
    add(re.sub(r"[··\s]", "", name or ""))
    toks = [t for t in re.split(r"[··\s()（）]+", name or "") if len(t) >= 2]
    for t in sorted(set(toks), key=len, reverse=True):
        add(t)
    return seen


def fetch_rules_for_org(s, name, apba_id, divis):
    """apbaId로 정확 필터한 기관 규정 목록. 정식명 0건이면 부분문자열 폴백.

    반환: (rules[list], used_term[str|None]).
    """
    for term in _search_terms(name):
        try:
            items = ac.fetch_all_rules(s, term, divis)
        except Exception:
            items = []
        hits = [it for it in items if it.get("apbaId") == apba_id]
        if hits:
            return hits, (None if term == name else term)
    return [], None


# ─────────────────────────────────────────────────────────
# 매칭
# ─────────────────────────────────────────────────────────
def _norm(t):
    return re.sub(r"\s+", "", t or "")


def _core(rule):
    """규정 title에서 기관명(pname) 접두를 제거한 핵심 명칭(공백 제거).

    'OO직제규정시행세칙'처럼 기관명이 접두로 붙거나, 기관명에 '정원관리'·'업무'
    등 매칭 키워드가 섞여 오탐/누락을 유발하는 문제를 제거한다.
    """
    tn = re.sub(r"\s+", "", rule.get("title") or "")
    pn = re.sub(r"\s+", "", rule.get("pname") or "")
    if pn and tn.startswith(pn):
        return tn[len(pn):]
    return tn


def match_jeonggwan(jg_rules):
    def is_jg(r):
        c = _core(r)
        return c == "정관" or c.endswith("정관") or "정관(" in c
    cands = [r for r in jg_rules if is_jg(r)]
    if not cands:
        return None
    exact = [r for r in cands if _core(r) == "정관"]
    return exact[0] if exact else min(cands, key=lambda r: len(_core(r)))


def match_jikje(jikje_rules):
    # 후보는 K1300(직제) 분류로 한정되어 이미 조직구조 규정군이다.
    pairs = [(r, _core(r)) for r in jikje_rules]

    def base_ok(c):
        return (("직제" in c or "조직" in c) and ("규정" in c or "규칙" in c)
                and "시행" not in c and "세칙" not in c)

    # 1) 정확 명칭(직제규정 > 직제규칙 > 조직규정 > 조직규칙 > 직제 > 조직)
    for exact in ("직제규정", "직제규칙", "조직규정", "조직규칙", "직제", "조직"):
        for r, c in pairs:
            if c == exact:
                return r
    # 2) 직제/조직으로 시작 + 규정/규칙 (규정을 규칙보다 우선)
    for want in ("규정", "규칙"):
        for r, c in pairs:
            if (c.startswith("직제") or c.startswith("조직")) and want in c and base_ok(c):
                return r
    # 3) 직제/조직 포함 + 규정/규칙 ('직제 및 업무분장 규정' 등, 규정 우선)
    for want in ("규정", "규칙"):
        for r, c in pairs:
            if want in c and base_ok(c):
                return r
    return None


def _sihaeng_level(c):
    if "직제" in c and "시행세칙" in c:
        return 5
    if "직제" in c and ("시행규칙" in c or "시행규정" in c):
        return 4
    if "조직" in c and any(k in c for k in ("시행세칙", "시행규칙", "시행규정")):
        return 3
    if "직제" in c and "세칙" in c:
        return 2
    if "조직" in c and "세칙" in c:
        return 2
    return 0


def match_sihaeng(jikje_rules, base_rule):
    base_seq = base_rule.get("seq") if base_rule else None
    scored = []
    for r in jikje_rules:
        if r.get("seq") == base_seq:
            continue
        c = _core(r)
        lv = _sihaeng_level(c)
        if lv:
            scored.append((r, c, lv))
    if not scored:
        return None, False
    clean = [x for x in scored if x[1].startswith("직제") or x[1].startswith("조직")]
    if clean:
        clean.sort(key=lambda x: (-x[2], len(x[1])))
        return clean[0][0], False
    # 깨끗한 후보 없고 접두형(부서별) 다수 → 모호
    if len(scored) > 1:
        return None, True
    return scored[0][0], False


# ─────────────────────────────────────────────────────────
# 임원 본문 파싱
# ─────────────────────────────────────────────────────────
def _after(row, key):
    try:
        i = row.index(key)
        return row[i + 1] if i + 1 < len(row) else ""
    except ValueError:
        return ""


def parse_execs(rep, org, dno, idate):
    tables = rep.get("표") or []
    rows = []
    for tbl in tables:
        if not tbl or not tbl[0]:
            continue
        head = tbl[0]
        if head[0] != "직위" or "성명" not in head:
            continue
        d = {c: "" for c in EXEC_COLS}
        d["기관명"], d["기관유형"], d["주무부처"] = org["name"], org["type"], org["dept"]
        d["공시기준일"], d["공시번호"] = idate or "", dno or ""
        d["직위"] = _after(head, "직위")
        d["성명"] = _after(head, "성명")
        for r in tbl[1:]:
            if not r:
                continue
            k = r[0]
            if k == "직책":
                d["직책"] = _after(r, "직책")
                d["성별"] = _after(r, "성별")
            elif k == "임기":
                d["임기시작"] = _after(r, "(시작일)")
                d["임기종료"] = _after(r, "(종료일)")
            elif k == "주요경력":
                d["주요경력"] = r[1] if len(r) > 1 else ""
            elif k == "선임절차":
                d["선임절차"] = r[1] if len(r) > 1 else ""
            elif k == "선임절차규정":
                d["선임근거규정"] = r[1] if len(r) > 1 else ""
            elif k == "당연직여부":
                d["당연직여부"] = _after(r, "당연직여부")
                d["임명권자"] = _after(r, "임명권자")
        rows.append(d)
    return rows


# ─────────────────────────────────────────────────────────
# 다운로드
# ─────────────────────────────────────────────────────────
def _exists_ok(path, minsize):
    return os.path.exists(path) and os.path.getsize(path) >= minsize


def _rule_files_all(s, seq):
    """findRuleDtl bFiles 전체(.zip 포함)를 파싱. fetch_rule_detail은 .zip 제외."""
    url = f"{ac.BASE_URL}/occasional/findRuleDtl.json"
    try:
        r = ac.retry_request(s, "GET", url, params={"seq": seq}, timeout=20)
        b = (r.json().get("data") or {}).get("bFiles", "") or ""
    except Exception:
        return []
    out = []
    for e in b.split(","):
        e = e.strip()
        if "|" in e:
            no, nm = e.split("|", 1)
            out.append({"file_no": no.strip(), "file_name": nm.strip()})
    return out


def download_rule(s, rule, folder, label, overwrite):
    if not rule:
        return "미매칭"
    det = ac.fetch_rule_detail(s, rule["seq"])
    latest = det.get("latest")
    if latest:
        ext = os.path.splitext(latest.get("file_name") or "")[1] or ".hwp"
        target = os.path.join(folder, f"{label}{ext}")
        if (not overwrite) and _exists_ok(target, MIN_RULE):
            return f"이미있음:{rule.get('title')}"
        ok, path, msg = ac.download_rule_file_to_path(s, latest["file_no"], target)
        if ok and _exists_ok(path, MIN_RULE):
            return f"OK:{rule.get('title')}"
        try:
            if path and os.path.exists(path) and os.path.getsize(path) < MIN_RULE:
                os.remove(path)
        except OSError:
            pass
        return f"실패:{msg}"
    # 비-zip 파일 없음 → .zip 첨부 폴백(분할·버전 모두 보존)
    zips = [f for f in _rule_files_all(s, rule["seq"])
            if (f["file_name"] or "").lower().endswith(".zip")]
    if not zips:
        return f"파일없음:{rule.get('title')}"
    if (not overwrite) and _exists_ok(os.path.join(folder, f"{label}.zip"), 300):
        return f"이미있음(zip):{rule.get('title')}"
    # 최신 버전만(분할 파트는 file_no가 인접 → 윈도우로 함께, 과거 버전은 제외)
    zips.sort(key=lambda f: int(f["file_no"]))
    max_no = int(zips[-1]["file_no"])
    keep = [f for f in zips if max_no - int(f["file_no"]) <= 20]
    okc = 0
    for i, zf in enumerate(keep):
        tgt = os.path.join(folder, f"{label}.zip" if i == 0 else f"{label}_{i+1}.zip")
        ok, p, _ = ac.download_rule_file_to_path(s, zf["file_no"], tgt)
        if ok and _exists_ok(p, 300):
            okc += 1
    return f"OK(zip):{rule.get('title')}" if okc else f"실패(zip):{rule.get('title')}"


def process(org, do_exec, overwrite_rules):
    s = sess()
    name, aid = org["name"], org["id"]
    folder = os.path.join(OUT_ROOT, ac.sanitize_filename(name, max_len=120))
    os.makedirs(folder, exist_ok=True)

    rec = {c: "" for c in MANIFEST_COLS}
    rec.update({"기관ID": aid, "기관명": name,
                "기관유형": org["type"], "주무부처": org["dept"]})
    miss = []
    execs = []

    # ── 임원현황 ──
    if do_exec:
        dno, idate = latest_disclosure(s, aid)
        if dno:
            rec["공시번호"], rec["공시기준일"] = dno, idate
            target = os.path.join(folder, "임원현황.pdf")
            if _exists_ok(target, MIN_PDF):
                rec["임원현황"] = "이미있음"
            else:
                ok, path, msg = ac.download_attachment(
                    s, "pdf", {"name": "임원현황.pdf"}, folder, disclosure_no=dno)
                rec["임원현황"] = "OK" if (ok and _exists_ok(path, MIN_PDF)) else f"실패:{msg}"
            try:
                rep = ac.fetch_report_tables(s, dno)
                if isinstance(rep, dict) and "표" in rep:
                    execs = parse_execs(rep, org, dno, idate)
            except Exception as e:
                rec["임원현황"] += f"|본문오류:{e}"
            rec["임원수"] = len(execs)
        else:
            rec["임원현황"] = "공시없음"
            rec["임원수"] = 0
    else:
        # 규정 전용 모드: 기존 PDF 존재 여부만 표기
        rec["임원현황"] = "이미있음" if _exists_ok(
            os.path.join(folder, "임원현황.pdf"), MIN_PDF) else "건너뜀"

    # 규정 재수집(overwrite) 시 기존 규정 파일 선제거 → 미매칭 전환 시 stale 방지
    # '직제규정*'는 '직제규정시행세칙'까지 잡으므로 '.*'와 '_*.zip' 두 패턴만 사용
    if overwrite_rules:
        for lbl in ("정관", "직제규정", "직제규정시행세칙"):
            for pat in (f"{lbl}.*", f"{lbl}_*.zip"):
                for old in glob.glob(os.path.join(folder, pat)):
                    try:
                        os.remove(old)
                    except OSError:
                        pass

    # ── 내부규정 (apbaId 정확 필터) ──
    k1500, term1 = fetch_rules_for_org(s, name, aid, "K1500")
    k1300, term2 = fetch_rules_for_org(s, name, aid, "K1300")
    used_term = term1 or term2
    # 정관: K1500 우선, 없으면 전체 분류에서 폴백
    jg = match_jeonggwan(k1500)
    jg_pool = k1500
    if jg is None:
        allr, term3 = fetch_rules_for_org(s, name, aid, "")
        jg = match_jeonggwan(allr)
        jg_pool = allr if allr else k1500
        used_term = used_term or term3
    rec["규정검색어"] = used_term or ""

    base = match_jikje(k1300)
    sih, ambiguous = match_sihaeng(k1300, base)

    cand_jg = " | ".join(r.get("title", "") for r in jg_pool)
    cand_jj = " | ".join(r.get("title", "") for r in k1300)

    plan = [("정관", jg), ("직제규정", base), ("직제규정시행세칙", sih)]
    for label, rule in plan:
        status = download_rule(s, rule, folder, label, overwrite_rules)
        if label == "직제규정시행세칙" and rule is None and ambiguous:
            status = "모호(부서별다수)"
        rec[label] = status
        bad = (status == "미매칭" or status.startswith("파일없음")
               or status.startswith("실패") or status.startswith("모호"))
        if bad:
            miss.append({"기관ID": aid, "기관명": name, "기관유형": org["type"],
                         "항목": label, "상태": status,
                         "정관_후보": cand_jg, "직제_후보": cand_jj})

    return rec, miss, execs


# ─────────────────────────────────────────────────────────
def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_excel(path, exec_rows):
    try:
        from openpyxl import Workbook
    except ImportError:
        alt = path.replace(".xlsx", ".csv")
        write_csv(alt, EXEC_COLS, exec_rows)
        return alt
    wb = Workbook()
    ws = wb.active
    ws.title = "임원현황"
    ws.append(EXEC_COLS)
    for r in exec_rows:
        ws.append([r.get(c, "") for c in EXEC_COLS])
    ws.freeze_panes = "A2"
    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--overwrite-rules", action="store_true",
                    help="규정 파일을 재매칭하여 덮어쓴다(임원현황 PDF는 유지).")
    ap.add_argument("--rules-only", action="store_true",
                    help="임원현황 단계를 건너뛰고 규정만 처리(통합 Excel 미갱신).")
    args = ap.parse_args()
    do_exec = not args.rules_only

    os.makedirs(OUT_ROOT, exist_ok=True)
    roster = get_roster(sess())
    print(f"[로스터] rootNo {ROOT_NO} 공시 기관 {len(roster)}개")
    write_csv(os.path.join(OUT_ROOT, "_기관로스터.csv"),
              ["id", "name", "type", "dept"], roster)

    if args.ids:
        want = set(x.strip() for x in args.ids.split(",") if x.strip())
        roster = [o for o in roster if o["id"] in want]
    if args.limit:
        roster = roster[:args.limit]
    print(f"[대상] {len(roster)}개  워커 {args.workers}  "
          f"임원현황={'O' if do_exec else 'X'}  규정덮어쓰기={'O' if args.overwrite_rules else 'X'}")

    manifest, misses, execs = [], [], []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, o, do_exec, args.overwrite_rules): o for o in roster}
        for fut in as_completed(futs):
            o = futs[fut]
            try:
                rec, miss, ex_rows = fut.result()
            except Exception as e:
                rec = {"기관ID": o["id"], "기관명": o["name"], "기관유형": o["type"],
                       "주무부처": o["dept"], "임원현황": f"처리오류:{e}", "임원수": 0}
                miss, ex_rows = [], []
            manifest.append(rec)
            misses.extend(miss)
            execs.extend(ex_rows)
            done += 1
            if done % 20 == 0 or done == len(roster):
                print(f"  진행 {done}/{len(roster)}  ({time.time()-t0:.0f}s)")

    manifest.sort(key=lambda r: r.get("기관명", ""))
    execs.sort(key=lambda r: (r.get("기관명", ""), r.get("직위", "")))

    write_csv(os.path.join(OUT_ROOT, "_수집매니페스트.csv"), MANIFEST_COLS, manifest)
    write_csv(os.path.join(OUT_ROOT, "_미매칭_규정_리포트.csv"), MISS_COLS, misses)
    if do_exec:
        xls = write_excel(os.path.join(OUT_ROOT, "_임원현황_통합.xlsx"), execs)
    else:
        xls = "(미갱신)"

    def cnt(col, pred):
        return sum(1 for r in manifest if pred(str(r.get(col, ""))))
    print("\n========== 수집 요약 ==========")
    print(f"기관 {len(manifest)}개 | 임원 통합행 {len(execs)}건 → {os.path.basename(str(xls))}")
    for col in ["임원현황", "정관", "직제규정", "직제규정시행세칙"]:
        ok = cnt(col, lambda v: v.startswith("OK") or v.startswith("이미있음"))
        miss = cnt(col, lambda v: v == "미매칭" or v.startswith("모호"))
        fail = cnt(col, lambda v: v.startswith("실패") or v.startswith("파일없음")
                   or v == "공시없음")
        print(f"  {col:10s}  성공 {ok:4d}  미매칭/모호 {miss:4d}  실패/없음 {fail:4d}")
    print(f"미매칭/실패 리포트 항목: {len(misses)}건")
    print(f"출력 폴더: {OUT_ROOT}")


if __name__ == "__main__":
    main()
