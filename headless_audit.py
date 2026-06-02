# -*- coding: utf-8 -*-
"""alio-mcp 헤드리스 전수 검증 (Phase 1)

크롤러 GUI를 띄우지 않고 정본 alio_core를 직접 두드려, 92개 전 항목 · 4종
엔드포인트 · 첨부파일 수집이 실제로 되는지 전수 검증한다.

- 정본 alio_core(alio-mcp) import → MCP 도구가 쓰는 코드 경로를 검증.
- precise_audit.py(크롤러)의 type별 probe 5종(jung/audit/envlaw/mgmt_eval/rule)을
  이식. 다운로드는 raw HTTP로 매직바이트까지 확인(실제 수신 검증), boilerplate
  회피는 정본 fetch_report_tables(itemReportRight.do)로 교차 확인.
- _item_meta_to_legacy(크롤러 GUI 메서드)의 type 매핑을 순수함수로 재구현.
- Verdict 판정: SUCCESS / EMPTY_VALID(정상 빈데이터) / FAILURE / 특수 7종.
  "정상 빈데이터"와 "진짜 실패"를 분리 — baseline 대비 신규 FAILURE만 회귀 경보.
- 모든 다운로드는 tempfile → 종료 시 rmtree(영구파일 안 남김, 읽기 전용).

실행:
  python3 headless_audit.py --stage=1                  # 산단공 92항목 broad
  python3 headless_audit.py --stage=2                  # 엔드포인트 4종 × 대표기관 deep
  python3 headless_audit.py --stage=all --baseline=baseline.json
  python3 headless_audit.py --items=21201,43006        # 특정 항목만
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alio_core as core  # 정본(alio-mcp) — fetch_report_tables 포함

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

TEST_APBA_ID = "C0208"
TEST_APBA_NAME = "한국산업단지공단"
JSON_CT = {"Content-Type": "application/json;charset=UTF-8"}
BASELINE_DEFAULT = os.path.join(HERE, "audit_baseline.json")


class Verdict(str, Enum):
    SUCCESS = "SUCCESS"                              # 자료 있고 다운로드/본문 OK
    EMPTY_VALID = "EMPTY_VALID"                      # 이 기관에 자료가 정상적으로 없음
    FAILURE = "FAILURE"                              # 자료 있는데 받기/파싱 실패(회귀 경보)
    SPECIAL_PDF_BOILERPLATE = "SPECIAL_PDF_BOILER"   # pdf 더미지만 HTML엔 실데이터(실질 OK)
    SPECIAL_SPATH_YEAR_404 = "SPECIAL_SPATH_404"     # spath 연도누락 404(알리오 데이터결함)
    SPECIAL_DFILE_HTTP500 = "SPECIAL_DFILE_500"      # dfile 서버오류
    SPECIAL_70401 = "SPECIAL_70401"                  # 사망자수 특수(dfile) 미수신
    SPECIAL_DUMMY_DISCNO = "SPECIAL_DUMMY_DISCNO"    # disclosureNo 더미(0000..)
    SPECIAL_HTML_BOUNDARY = "SPECIAL_HTML_BOUNDARY"  # HTML 표 경계 파싱 경계
    SPECIAL_EXTLINK_ONLY = "SPECIAL_EXTLINK_ONLY"    # 첨부 없이 외부링크만


# baseline 회귀 경보 대상(이 Verdict가 신규로 늘면 빨간불)
REGRESSION_VERDICTS = {Verdict.FAILURE.value}


# ─────────────────────────────────────────────────────────
# type 매핑 — _item_meta_to_legacy(크롤러) 순수함수 재구현
# ─────────────────────────────────────────────────────────
def item_type(item_meta) -> str:
    root_no = core.build_item_root_no(item_meta)
    mcd = item_meta.get("mcd", "") or ""
    kind = core.detect_endpoint_kind(item_meta)

    # 1) rootNo 완전 일치
    for _, info in core.DISCLOSURE_ITEMS.items():
        if info["rootNo"] and info["rootNo"] == root_no:
            return info["type"]
    # 2) 내부규정
    if mcd == "21110":
        return "rule"
    # 3) 다중 rootNos 집합 일치
    new_set = {x.strip() for x in root_no.split(",") if x.strip()}
    for _, info in core.DISCLOSURE_ITEMS.items():
        legacy_set = {x.strip() for x in (info["rootNo"] or "").split(",") if x.strip()}
        if legacy_set and legacy_set == new_set:
            return info["type"]
    # 4) 항목명 일치(괄호 앞 기준)
    scdnm = (item_meta.get("scdnm") or "").strip()
    mcdnm = (item_meta.get("mcdnm") or "").strip()
    for name, info in core.DISCLOSURE_ITEMS.items():
        base = name.split("(")[0].strip()
        if base and (base == scdnm or base == mcdnm):
            return info["type"]
    # 5) 신규
    if kind == "rule":
        return "rule"
    if (item_meta.get("reportYn") or "").upper() == "N":
        return "envlaw"
    return "jung"


def _blank():
    return {"records": 0, "pdf": 0, "files": 0, "files_total": 0,
            "html_tables": 0, "extlinks": 0, "dno": "", "err": "",
            "http_errs": [], "dummy_dno": False, "disclosed": True,
            "private": False, "kind": ""}


# ─────────────────────────────────────────────────────────
# probe 5종 (precise_audit 이식 + apba 파라미터화 + 정본 교차)
# ─────────────────────────────────────────────────────────
def jung_probe(sess, root_no, save_dir, apba_id, apba_name):
    """itemOrganListJung → disclosureNo/files → pdf.json + file.json + (HTML 표)."""
    pr = _blank()
    url = f"{core.BASE_URL}/item/itemOrganListJung.json"
    body = {"reportFormRootNo": root_no, "apbaType": [], "jidtDptm": [],
            "area": [], "apba_id": "", "pageNo": 1}
    try:
        r = core.retry_request(sess, "POST", url, json=body, headers=JSON_CT, timeout=15)
        d = r.json()
    except Exception as e:
        pr["err"] = f"itemOrganListJung: {e}"
        return pr
    if d.get("status") != "success":
        pr["err"] = "API status != success"
        return pr
    organs = d.get("data", {}).get("organList", [])
    if isinstance(organs, dict):
        organs = organs.get("result", [])
    matches = [o for o in organs if o.get("apbaId") == apba_id]
    if not matches:
        return pr  # records=0 → EMPTY_VALID
    case = matches[0]
    pr["records"] = 1
    dno = (case.get("disclosureNo") or "").strip()
    files_str = case.get("files", "") or ""
    apba_type = case.get("apbaType", "")

    if not dno:  # fetch_disclosures default fallback
        for try_rn in [x.strip() for x in root_no.split(",") if x.strip()]:
            try:
                sr = sess.post(f"{core.BASE_URL}/item/itemReportListSusi.json",
                               json={"pageNo": 1, "apbaId": apba_id, "apbaType": apba_type,
                                     "reportFormRootNo": try_rn, "search_word": "",
                                     "search_flag": "title", "bid_type": "", "enfc_istt": ""},
                               headers=JSON_CT, timeout=15)
                rl = sr.json().get("data", {}).get("result", []) or []
                if rl:
                    dno = str(rl[0].get("disclosureNo", "") or "")
                    if dno:
                        break
            except Exception:
                continue
    pr["dno"] = dno or "(없음)"
    # itemOrganListJung은 공시대상 전체 355개 기관 행을 항상 반환한다.
    # 실제 공시 여부는 disclosureNo 유무로 판정(미공시면 dno 빈값) — 이 기관이
    # 이 항목을 공시하지 않았으면 받을 자료가 없으므로 EMPTY_VALID.
    pr["disclosed"] = bool(dno)
    if dno and len(dno) < 5:
        pr["dummy_dno"] = True

    # PDF (raw, 매직바이트 확인)
    if dno:
        try:
            resp = sess.get(f"{core.BASE_URL}/download/pdf.json?disclosureNo={dno}", timeout=30)
            if resp.status_code == 200:
                ct = (resp.headers.get("Content-Type") or "").lower()
                if "pdf" in ct or (len(resp.content) > 100 and resp.content[:4] == b"%PDF"):
                    pr["pdf"] = 1
            else:
                pr["http_errs"].append(resp.status_code)
        except Exception:
            pass
        # boilerplate 회피: 정본 fetch_report_tables(itemReportRight.do) 교차
        try:
            rt = core.fetch_report_tables(sess, dno)
            if "error" not in rt:
                pr["html_tables"] = rt.get("표_개수", 0)
        except Exception:
            pass

    # files (raw)
    parsed = core.parse_files_field(files_str) if files_str else []
    pr["files_total"] = len(parsed)
    for fp in parsed[:3]:
        fid = fp.get("id", "")
        if not fid or not dno:
            continue
        try:
            fr = sess.get(f"{core.BASE_URL}/download/file.json?f={fid}&d={dno}", timeout=20)
            if fr.status_code == 200 and len(fr.content) > 100:
                if "html" not in (fr.headers.get("Content-Type") or "").lower():
                    pr["files"] += 1
            elif fr.status_code != 200:
                pr["http_errs"].append(fr.status_code)
        except Exception:
            pass
    return pr


def audit_probe(sess, root_no, save_dir, apba_id, apba_name):
    """itemReportListSusi → itemReportFiles.json → dfile.json."""
    pr = _blank()
    try:
        r = core.retry_request(sess, "POST", f"{core.BASE_URL}/item/itemReportListSusi.json",
                               json={"pageNo": 1, "apbaId": apba_id, "apbaType": "",
                                     "reportFormRootNo": root_no, "search_word": "",
                                     "search_flag": "title", "bid_type": "", "enfc_istt": ""},
                               headers=JSON_CT, timeout=15)
        rl = r.json().get("data", {}).get("result", []) or []
    except Exception as e:
        pr["err"] = str(e)
        return pr
    if not rl:
        return pr
    pr["records"] = len(rl)
    first = rl[0]
    dno = (first.get("disclosureNo") or "").strip()
    sno = (first.get("submissionNo") or "").strip()
    if not dno or not sno:
        pr["err"] = "dno/sno 없음"
        return pr
    try:
        fr = sess.get(f"{core.BASE_URL}/item/itemReportFiles.json?disclosureNo={dno}", timeout=15)
        if fr.status_code != 200:
            pr["http_errs"].append(fr.status_code)
            pr["err"] = f"itemReportFiles HTTP {fr.status_code}"
            return pr
        files_data = fr.json().get("data", []) or []
    except Exception as e:
        pr["err"] = f"itemReportFiles: {e}"
        return pr
    if not files_data:
        pr["err"] = "itemReportFiles 빈 응답"
        return pr
    pr["files_total"] = len(files_data)
    for fi in files_data[:1]:
        fname = fi.get("orcpFileNa", "")
        if not fname:
            continue
        try:
            dr = sess.get(f"{core.BASE_URL}/download/dfile.json"
                          f"?fileName={quote(fname, safe='')}&submissionNo={sno}", timeout=30)
            if dr.status_code == 200 and len(dr.content) > 100:
                if "html" not in (dr.headers.get("Content-Type") or "").lower():
                    pr["files"] += 1
            elif dr.status_code != 200:
                pr["http_errs"].append(dr.status_code)
        except Exception:
            pass
    if not pr["files"] and not pr["err"]:
        pr["err"] = "dfile 다운로드 실패"
    return pr


def envlaw_probe(sess, root_no, save_dir, apba_id, apba_name):
    """itemReportListSusi → 정본 fetch_board_attachment_list → download_board_attachment.
    입찰공고 등은 자료마다 첨부/외부링크 유무가 달라 상위 5건을 확인한다. 첨부가
    있으면 다운로드 검증하고, 없으면 외부링크(g2b 위임)·비공개(정보공개법 제9조)
    여부를 본다 — 둘 다 첨부 없음이 정상인 케이스."""
    pr = _blank()
    try:
        r = core.retry_request(sess, "POST", f"{core.BASE_URL}/item/itemReportListSusi.json",
                               json={"pageNo": 1, "apbaId": apba_id, "apbaType": "",
                                     "reportFormRootNo": root_no, "search_word": "",
                                     "search_flag": "title", "bid_type": "", "enfc_istt": ""},
                               headers=JSON_CT, timeout=15)
        rl = (r.json().get("data") or {}).get("result", []) or []
    except Exception as e:
        pr["err"] = str(e)
        return pr
    if not rl:
        return pr
    pr["records"] = len(rl)
    ext_total = 0
    private = False
    for cand in rl[:5]:
        vmeta = {
            "report_form_no": cand.get("reportFormNo", ""),
            "table_name": cand.get("tableName", ""),
            "idx_name": cand.get("idxName", ""),
            "idx": cand.get("idx", ""),
            "submission_no": cand.get("submissionNo", ""),
            "bid_type": cand.get("bidType", "") or "",
            "disclosure_no": cand.get("disclosureNo", ""),
        }
        try:
            atts = core.fetch_board_attachment_list(sess, apba_id, vmeta)
        except Exception:
            atts = []
        if atts:  # 첨부 발견 → 다운로드 검증하고 종료
            pr["kind"] = atts[0].get("kind", "")
            pr["files_total"] = len(atts)
            try:
                ok, _, msg = core.download_board_attachment(sess, atts[0], save_dir)
                if ok:
                    pr["files"] = 1
                else:
                    pr["err"] = msg or "다운로드 실패"
                    if re.search(r"\b404\b", str(msg) or ""):
                        pr["http_errs"].append(404)
            except Exception as e:
                pr["err"] = f"download_board_attachment: {e}"
            return pr
        # 첨부 없음 → 외부링크/비공개 확인
        try:
            ext = core.fetch_board_external_links(sess, apba_id, vmeta)
            ext_total = max(ext_total, len(ext or []))
        except Exception:
            pass
        if cand.get("originalPrivateContent") or ("비공개" in json.dumps(cand, ensure_ascii=False)):
            private = True
        if ext_total or private:
            break
    pr["extlinks"] = ext_total
    pr["private"] = private
    pr["err"] = "외부링크 위임" if ext_total else ("비공개(정보공개법 제9조)" if private else "첨부 0개")
    return pr


def mgmt_eval_probe(sess, root_no, save_dir, apba_id, apba_name):
    """itemReportListSusi → itemBoard{rfn}.do HTML → download.json?fileNo."""
    pr = _blank()
    try:
        r = core.retry_request(sess, "POST", f"{core.BASE_URL}/item/itemReportListSusi.json",
                               json={"pageNo": 1, "apbaId": apba_id, "apbaType": "",
                                     "reportFormRootNo": root_no, "search_word": "",
                                     "search_flag": "title", "bid_type": "", "enfc_istt": ""},
                               headers=JSON_CT, timeout=15)
        rl = r.json().get("data", {}).get("result", []) or []
    except Exception as e:
        pr["err"] = str(e)
        return pr
    if not rl:
        return pr
    pr["records"] = len(rl)
    first = rl[0]
    dno = (first.get("disclosureNo") or "").strip()
    rfn = first.get("reportFormNo", "") or root_no
    table_name = first.get("tableName", "") or ""
    idx_val = first.get("idx", "") or ""
    if not dno:
        pr["err"] = "disclosureNo 없음"
        return pr
    detail_url = (f"{core.BASE_URL}/item/itemBoard{rfn}.do?disclosureNo={dno}"
                  f"&apbaId={apba_id}&nowcode={rfn}&reportFormNo={rfn}"
                  f"&table_name={table_name}&idx_name=BOARD_NO&idx={idx_val}"
                  f"&reportGbn=N&bid_type=0")
    try:
        dr = sess.get(detail_url, timeout=30)
        if dr.status_code != 200:
            pr["http_errs"].append(dr.status_code)
            pr["err"] = f"상세 HTTP {dr.status_code}"
            return pr
    except Exception as e:
        pr["err"] = f"상세페이지: {e}"
        return pr
    links = re.findall(r'href="/download/download\.json\?fileNo=(\d+)"[^>]*>([^<]+)</a>', dr.text)
    if not links:
        pr["err"] = "HTML 첨부 0개"
        return pr
    pr["files_total"] = len(links)
    fno = links[0][0]
    try:
        fr = sess.get(f"{core.BASE_URL}/download/download.json", params={"fileNo": fno}, timeout=30)
        if fr.status_code == 200 and len(fr.content) > 100:
            if "html" not in (fr.headers.get("Content-Type") or "").lower():
                pr["files"] = 1
        elif fr.status_code != 200:
            pr["http_errs"].append(fr.status_code)
    except Exception:
        pass
    if not pr["files"] and not pr["err"]:
        pr["err"] = "download.json 수신 실패"
    return pr


def rule_probe(sess, root_no, save_dir, apba_id, apba_name):
    """findRuleList → findRuleDtl(bFiles) → rulefiledown.json (정관 K1500 1건)."""
    pr = _blank()
    try:
        r = sess.get(f"{core.BASE_URL}/occasional/findRuleList.json",
                     params={"type": "apbaNa", "word": apba_name, "pageNo": 1, "divis": "K1500"},
                     timeout=15)
        rules = r.json().get("data", {}).get("result", []) or []
    except Exception as e:
        pr["err"] = str(e)
        return pr
    if not rules:
        return pr  # 이 기관 정관 미제출 → EMPTY_VALID
    pr["records"] = len(rules)
    seq = rules[0].get("seq", "")
    if not seq:
        pr["err"] = "seq 없음"
        return pr
    try:
        dr = sess.get(f"{core.BASE_URL}/occasional/findRuleDtl.json", params={"seq": seq}, timeout=15)
        if dr.status_code != 200:
            pr["http_errs"].append(dr.status_code)
            pr["err"] = f"findRuleDtl HTTP {dr.status_code}"
            return pr
        b_files = dr.json().get("data", {}).get("bFiles", "") or ""
    except Exception as e:
        pr["err"] = f"findRuleDtl: {e}"
        return pr
    files = []
    for entry in b_files.split(","):
        if "|" in entry:
            fno, fname = entry.split("|", 1)
            if not fname.strip().lower().endswith(".zip"):
                files.append((fno.strip(), fname.strip()))
    if not files:
        pr["err"] = "유효 파일 없음"
        return pr
    pr["files_total"] = len(files)
    fno = files[0][0]
    try:
        rr = sess.get(f"{core.BASE_URL}/download/rulefiledown.json", params={"fileNo": fno}, timeout=30)
        if rr.status_code == 200 and len(rr.content) > 100:
            if "html" not in (rr.headers.get("Content-Type") or "").lower():
                pr["files"] = 1
        elif rr.status_code != 200:
            pr["http_errs"].append(rr.status_code)
    except Exception:
        pass
    if not pr["files"] and not pr["err"]:
        pr["err"] = "rulefiledown 수신 실패"
    return pr


PROBES = {"jung": jung_probe, "audit": audit_probe, "envlaw": envlaw_probe,
          "mgmt_eval": mgmt_eval_probe, "rule": rule_probe,
          # 나머지 type은 jung 흐름(itemOrganListJung) 재사용
          "general": jung_probe, "discipline": jung_probe,
          "integrity": jung_probe, "safety": jung_probe}


# ─────────────────────────────────────────────────────────
# Verdict 판정
# ─────────────────────────────────────────────────────────
def classify(root_no, pr) -> Verdict:
    rec = pr["records"]
    pdf, files, ht = pr["pdf"], pr["files"], pr["html_tables"]
    errs = pr.get("http_errs", [])
    if rec == 0:
        return Verdict.EMPTY_VALID
    if not pr.get("disclosed", True):
        return Verdict.EMPTY_VALID  # 목록 행은 있으나 미공시(disclosureNo 없음)
    if pr.get("dummy_dno"):
        return Verdict.SPECIAL_DUMMY_DISCNO
    if "70401" in (root_no or ""):
        return Verdict.SUCCESS if (pdf or files) else Verdict.SPECIAL_70401
    if pdf == 0 and files == 0:
        # pdf 더미인데 HTML엔 실데이터 → 실질 OK
        if ht > 0:
            return Verdict.SPECIAL_PDF_BOILERPLATE
        if pr.get("extlinks", 0) > 0:
            return Verdict.SPECIAL_EXTLINK_ONLY
        if pr.get("private"):
            return Verdict.EMPTY_VALID  # 비공개(정보공개법 제9조) — 첨부 없음이 정상
        if 500 in errs:
            return Verdict.SPECIAL_DFILE_HTTP500
        if 404 in errs:
            return Verdict.SPECIAL_SPATH_YEAR_404
        return Verdict.FAILURE
    return Verdict.SUCCESS


# ─────────────────────────────────────────────────────────
# 검증 실행
# ─────────────────────────────────────────────────────────
def probe_item(sess, item, apba_id, apba_name):
    name = core.build_item_display_name(item)
    rn = core.build_item_root_no(item)
    typ = item_type(item)
    fn = PROBES.get(typ, jung_probe)
    tmp = tempfile.mkdtemp(prefix="halio_")
    try:
        pr = fn(sess, rn, tmp, apba_id, apba_name)
    except Exception as e:
        pr = _blank()
        pr["err"] = f"probe 예외: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    v = classify(rn, pr)
    return {"name": name, "rn": rn, "type": typ, "lcdnm": item.get("lcdnm", ""),
            "verdict": v.value, "apba": apba_name, **pr}


def make_session():
    s = core.create_session(verify_ssl=False)  # SSL 검사(가로채기) 보안장비 환경 대응
    s.headers.update(core.HEADERS)
    return s


def resolve_apba(insts, kw):
    for nm, v in insts.items():
        if kw in nm:
            return v.get("apba_id", ""), nm
    return "", ""


def run_stage1(items, max_workers=8):
    sess = make_session()
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(probe_item, sess, it, TEST_APBA_ID, TEST_APBA_NAME) for it in items]
        for i, fut in enumerate(as_completed(futs)):
            results.append(fut.result())
            if (i + 1) % 15 == 0:
                print(f"  [stage1] {i+1}/{len(items)}", file=sys.stderr)
    return results


def run_stage2(items, max_workers=6):
    """엔드포인트 4종 대표항목 × 대표기관 교차."""
    sess = make_session()
    insts = core.load_public_institutions()
    orgs = [(TEST_APBA_ID, TEST_APBA_NAME)]
    for kw in ("한국전력공사", "한국국토정보공사", "한국가스공사"):
        aid, nm = resolve_apba(insts, kw)
        if aid:
            orgs.append((aid, nm))
    # 엔드포인트 4종을 대표하는 항목 rootNo
    rep_roots = {"10105", "43006", "B1270", "B1230", "21110", "21201"}
    by_root = {core.build_item_root_no(it): it for it in items}
    targets = []
    for rn in rep_roots:
        it = by_root.get(rn)
        if it:
            for aid, nm in orgs:
                targets.append((it, aid, nm))
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(probe_item, sess, it, aid, nm) for (it, aid, nm) in targets]
        for fut in as_completed(futs):
            results.append(fut.result())
    return results


# ─────────────────────────────────────────────────────────
# 출력 / baseline
# ─────────────────────────────────────────────────────────
def print_report(results, title):
    print("\n" + "=" * 120)
    print(f"[{title}]  총 {len(results)}건")
    print("=" * 120)
    print(f'{"항목명":34s} {"기관":14s} {"type":9s} {"verdict":20s} {"rec":>3s} {"pdf":>3s} {"file":>4s} {"htm":>3s} 비고')
    print("-" * 120)
    for r in sorted(results, key=lambda x: (x["verdict"], x["type"], x["name"])):
        bigo = r.get("err", "")
        if r.get("kind"):
            bigo = f"kind={r['kind']} {bigo}".strip()
        if r.get("http_errs"):
            bigo = f"HTTP{r['http_errs']} {bigo}".strip()
        print(f'{r["name"][:33]:34s} {r["apba"][:13]:14s} {r["type"]:9s} '
              f'{r["verdict"]:20s} {r["records"]:>3d} {r["pdf"]:>3d} '
              f'{r["files"]:>4d} {r["html_tables"]:>3d} {bigo[:34]}')


def print_stats(results):
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n[통계]")
    for v in Verdict:
        if counts.get(v.value):
            print(f"  {v.value:22s} {counts[v.value]}")
    failures = [r for r in results if r["verdict"] in REGRESSION_VERDICTS]
    print(f"\n[FAILURE {len(failures)}건]")
    for r in failures:
        print(f'  ✗ {r["name"][:30]:30s} {r["apba"][:12]:12s} type={r["type"]:9s} '
              f'rn={r["rn"]:18s} err={r.get("err","")}')
    return counts, failures


def load_baseline(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_baseline(path, results):
    snap = {f'{r["rn"]}@{r["apba"]}': r["verdict"] for r in results}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f"\nbaseline 저장: {path} ({len(snap)}건)")


def diff_baseline(baseline, results):
    if not baseline:
        return []
    regressions = []
    for r in results:
        key = f'{r["rn"]}@{r["apba"]}'
        old = baseline.get(key)
        new = r["verdict"]
        # 신규 FAILURE: 이전이 FAILURE가 아니었는데 지금 FAILURE
        if new in REGRESSION_VERDICTS and old not in REGRESSION_VERDICTS:
            regressions.append((r, old))
    return regressions


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stage", default="1", choices=["1", "2", "all"])
    p.add_argument("--items", default="", help="콤마 구분 mcd/rootNo/항목명 키워드")
    p.add_argument("--baseline", default="", help="비교/저장할 baseline json (없으면 저장만)")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()

    items = core.get_alio_items()
    if args.items:
        kws = [x.strip() for x in args.items.split(",") if x.strip()]
        items = [it for it in items
                 if it.get("mcd", "") in kws
                 or core.build_item_root_no(it) in kws
                 or any(kw in core.build_item_display_name(it) for kw in kws)]
        print(f"필터 적용: {len(items)}개 항목", file=sys.stderr)
    else:
        print(f"전체 {len(items)}개 항목", file=sys.stderr)

    all_results = []
    if args.stage in ("1", "all"):
        r1 = run_stage1(items, args.workers)
        print_report(r1, "Stage 1 — 산단공(C0208) 전 항목 broad")
        all_results += r1
    if args.stage in ("2", "all"):
        r2 = run_stage2(items, max(4, args.workers - 2))
        print_report(r2, "Stage 2 — 엔드포인트 4종 × 대표기관 deep")
        all_results += r2

    counts, failures = print_stats(all_results)

    baseline_path = args.baseline or BASELINE_DEFAULT
    baseline = load_baseline(baseline_path)
    regressions = diff_baseline(baseline, all_results)
    if baseline is None:
        save_baseline(baseline_path, all_results)
    else:
        print(f"\n[baseline 대비 신규 FAILURE: {len(regressions)}건]  ({baseline_path})")
        for r, old in regressions:
            print(f'  ⚠ {r["name"][:30]:30s} {r["apba"][:12]:12s} {old} → {r["verdict"]}')

    sys.exit(1 if regressions else 0)


if __name__ == "__main__":
    main()
