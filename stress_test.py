# -*- coding: utf-8 -*-
"""alio-mcp 스트레스 테스트 — 위탁집행형 준정부기관 전수 × 전 공시항목.

목적
  1) 모든 위탁집행형 준정부기관(49개)에 대해 전 공시항목(92개)을 확인하고
     첨부파일이 있으면 실제로 수집(저장)한다.
  2) 그 과정에서 발생하는 모든 이상(예외·HTTP오류·예상밖 빈응답·파싱실패)을
     전 셀 단위로 기록해 코드 버그를 가려낸다.

설계 원칙
  - **테스트 대상은 MCP 도구 표면**이다. alio_mcp.py의 @mcp.tool() 함수를 그대로
    호출한다(FastMCP 데코레이터는 원본 함수를 반환하므로 직접 호출 가능).
  - 보고서형(76): itemOrganListJung 1콜로 355기관 disclosureNo+files+submissionNo를
    받아 49개만 추린 뒤(열거 효율), 각 기관에 대해 실제 MCP 도구
    download_report / get_report_data / download_disclosure_attachment 를 호출.
  - 게시판형(14): 12종은 apbaId 필수 → 기관별 list_all_board_items →
    list_board_attachments → download_board_attachment.
  - 내부규정(21110): 기관별 list_rules → download_rule_file.
  - 사망자수(70401): 보고서형 흐름 + dfile(itemReportFiles.json로 파일명 발견 →
    download_disclosure_attachment kind='dfile').

분류(classification)
  SUCCESS         자료 있고 다운로드/본문 OK
  EMPTY_VALID     이 기관이 이 항목 미공시(정상)
  BOILERPLATE_OK  pdf는 더미지만 HTML 본문에 실데이터(실질 OK)
  EXTLINK_ONLY    첨부 대신 외부링크만(정상)
  SERVER_DEFECT   알리오 서버측 결함(spath 연도누락 404 / dfile 500 등) — 우리 책임 아님
  TOOL_ERROR      MCP 도구가 예상밖 {"error":...} 반환(조사 필요)
  CODE_EXCEPTION  도구 호출 중 파이썬 예외(=우리 코드 버그, 최우선)
  CAPPED          수량 상한으로 다운로드 생략(자료는 존재)

실행
  python3 stress_test.py                 # 전수
  python3 stress_test.py --items 21201,B1030  # 특정 항목만
  python3 stress_test.py --limit-orgs 5  # 앞 5개 기관만(스모크)
  python3 stress_test.py --no-download   # 다운로드 생략(열거·파싱 경로만)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import alio_core as core
import alio_mcp as M  # 테스트 대상: @mcp.tool() 함수들

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

JSON_CT = {"Content-Type": "application/json;charset=UTF-8"}

# ── 수집 상한(코드경로 커버리지는 보장하되 디스크/시간 폭주 방지) ──
BOARD_MAX_RECORDS_INSPECT = 10     # (기관,게시판항목)당 첨부 메타 조사할 레코드 수
                                   # (B1030 입찰공고는 기관당 수백~천건이라 전수 조사 시 폭주)
BOARD_MAX_RECORDS_DOWNLOAD = 3     # (기관,게시판항목)당 첨부 다운로드할 레코드 수
BOARD_MAX_ATTACH_PER_RECORD = 5    # 레코드당 다운로드할 첨부 수
REPORT_MAX_FILES_PER_CELL = 8      # 보고서 부속 file 다운로드 상한
RULE_DIVIS_TO_DOWNLOAD = "K1500"   # 내부규정은 정관 1건만 받아 경로 검증
BOILERPLATE_MAX = 40000            # pdf bytes 이하 + HTML표 있으면 boilerplate로 간주
HEAVY_BOARD_ROOTS = {"B1030"}      # 입찰공고는 기관당 수천건 → 전체순회 대신 1페이지만
                                   # (나머지 13종 게시판형은 list_all_board_items 전체순회 유지)

STAMP = datetime.now().strftime("%Y%m%d")
SAVE_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", f"alio_stress_{STAMP}")


# ─────────────────────────────────────────────────────────
# 대상 기관·항목 로드
# ─────────────────────────────────────────────────────────
def load_targets(org_filter="위탁집행", limit=0):
    insts = core.load_public_institutions()
    targets = [
        {"apba_id": v["apba_id"], "name": n, "type": v["inst_type"],
         "dept": v["dept"], "region": v["region"]}
        for n, v in sorted(insts.items())
        if org_filter in (v.get("inst_type") or "")
    ]
    return targets[:limit] if limit else targets


def classify_route(item):
    """항목 → 라우트(report / board / rule)."""
    mcd = item.get("mcd", "") or ""
    rn = core.build_item_root_no(item)
    if mcd == "21110":
        return "rule"
    if (item.get("reportYn") or "").upper() == "N" or rn.startswith("B"):
        return "board"
    return "report"


# ─────────────────────────────────────────────────────────
# 결과 레코드
# ─────────────────────────────────────────────────────────
def rec(apba_id, name, item_name, root_no, route, cls, **kw):
    r = {"apba_id": apba_id, "name": name, "item": item_name, "rootNo": root_no,
         "route": route, "cls": cls}
    r.update(kw)
    return r


def _make_session():
    s = core.create_session(verify_ssl=False)  # SSL 검사(가로채기) 보안장비 환경 대응
    s.headers.update(core.HEADERS)
    return s


# ─────────────────────────────────────────────────────────
# 보고서형 열거: itemOrganListJung 1콜 → {apba_id: {dno, files, sno, ...}}
# ─────────────────────────────────────────────────────────
def enum_report_organs(sess, root_no, want_ids):
    primary = root_no.split(",")[0].strip()
    url = f"{core.BASE_URL}/item/itemOrganListJung.json"
    body = {"reportFormRootNo": primary, "apbaType": [], "jidtDptm": [],
            "area": [], "apba_id": "", "pageNo": 1}
    try:
        r = core.retry_request(sess, "POST", url, json=body, headers=JSON_CT, timeout=20)
        d = r.json()
    except Exception as e:
        return {"__error__": f"itemOrganListJung 예외: {e}"}
    if d.get("status") and d.get("status") != "success":
        return {"__error__": f"API status={d.get('status')} msg={d.get('message')}"}
    organs = d.get("data", {}).get("organList", [])
    if isinstance(organs, dict):
        organs = organs.get("result", [])
    out = {}
    for o in (organs or []):
        aid = o.get("apbaId")
        if aid in want_ids:
            out[aid] = {
                "dno": (o.get("disclosureNo") or "").strip(),
                "sno": (o.get("submissionNo") or "").strip(),
                "files": o.get("files", "") or "",
                "apbaNa": o.get("apbaNa", ""),
            }
    return out


def _dl_dir(item_name, inst_name):
    d = os.path.join(SAVE_ROOT, core.sanitize_filename(item_name, 50),
                     core.sanitize_filename(inst_name, 50))
    os.makedirs(d, exist_ok=True)
    return d


# ─────────────────────────────────────────────────────────
# 보고서형 1셀 처리 (MCP 도구 호출)
# ─────────────────────────────────────────────────────────
def collect_report_cell(sess, item, target, info, do_download):
    """info: enum_report_organs 결과의 한 기관 dict. 없으면 미공시."""
    name = target["name"]
    item_name = core.build_item_display_name(item)
    rn = core.build_item_root_no(item)
    out = []
    dno = info.get("dno") if info else ""
    sno = info.get("sno") if info else ""
    files_raw = info.get("files", "") if info else ""

    if not dno:
        out.append(rec(target["apba_id"], name, item_name, rn, "report",
                       "EMPTY_VALID", note="disclosureNo 없음(미공시)"))
        return out

    save_dir = _dl_dir(item_name, name)

    # 1) 본문 HTML (get_report_data) — boilerplate 판별·파싱 경로
    html_tables = 0
    try:
        rd = M.get_report_data(dno)
        if isinstance(rd, dict) and "error" not in rd:
            html_tables = rd.get("표_개수", 0)
        elif isinstance(rd, dict):
            # EMPTY(순수 첨부 항목)는 정상일 수 있음
            pass
    except Exception as e:
        out.append(rec(target["apba_id"], name, item_name, rn, "report",
                       "CODE_EXCEPTION", step="get_report_data", dno=dno,
                       exc=f"{type(e).__name__}: {e}",
                       tb=traceback.format_exc()[-800:]))

    # 2) 보고서 PDF (download_report)
    pdf_ok = False
    pdf_size = 0
    if do_download:
        try:
            pr = M.download_report(dno, save_dir=save_dir,
                                   filename=f"{core.sanitize_filename(item_name,40)}_{name[:20]}_{dno}.pdf")
            if isinstance(pr, dict) and "error" not in pr:
                pdf_ok = True
                pdf_size = pr.get("size_bytes", 0)
            else:
                out.append(rec(target["apba_id"], name, item_name, rn, "report",
                               "TOOL_ERROR", step="download_report", dno=dno,
                               tool_error=(pr or {}).get("error")))
        except Exception as e:
            out.append(rec(target["apba_id"], name, item_name, rn, "report",
                           "CODE_EXCEPTION", step="download_report", dno=dno,
                           exc=f"{type(e).__name__}: {e}",
                           tb=traceback.format_exc()[-800:]))

    # 3) 부속 첨부 file (download_disclosure_attachment kind='file')
    parsed = core.parse_files_field(files_raw) if files_raw else []
    file_ok = 0
    file_fail = 0
    if do_download:
        for fp in parsed[:REPORT_MAX_FILES_PER_CELL]:
            fid, fname = fp.get("id", ""), fp.get("name", "")
            if not fid or not fname:
                continue
            # .zip 등도 받아본다(실제 첨부 수집)
            try:
                fr = M.download_disclosure_attachment(
                    "file", fileName=fname, disclosureNo=dno, fileId=fid, save_dir=save_dir)
                if isinstance(fr, dict) and "error" not in fr and fr.get("size_bytes", 0) > 0:
                    file_ok += 1
                else:
                    file_fail += 1
                    out.append(rec(target["apba_id"], name, item_name, rn, "report",
                                   "TOOL_ERROR", step="download_disclosure_attachment(file)",
                                   dno=dno, fileId=fid, fname=fname[:60],
                                   tool_error=(fr or {}).get("error")))
            except Exception as e:
                file_fail += 1
                out.append(rec(target["apba_id"], name, item_name, rn, "report",
                               "CODE_EXCEPTION", step="download_disclosure_attachment(file)",
                               dno=dno, fileId=fid, fname=fname[:60],
                               exc=f"{type(e).__name__}: {e}",
                               tb=traceback.format_exc()[-800:]))

    # 4) 70401 사망자수 → dfile (안전경영책임보고서)
    is_70401 = "70401" in rn
    dfile_note = ""
    if is_70401 and do_download:
        dfile_note = _try_dfile(sess, dno, sno, save_dir, out, target, item_name, rn, name)

    # 셀 종합 분류(대표 1건) — 다운로드 성공/boilerplate/실데이터 종합
    if pdf_ok or file_ok or html_tables > 0:
        if pdf_ok and pdf_size and pdf_size <= BOILERPLATE_MAX and html_tables > 0 and file_ok == 0:
            cls = "BOILERPLATE_OK"
        else:
            cls = "SUCCESS"
        out.append(rec(target["apba_id"], name, item_name, rn, "report", cls,
                       step="summary", dno=dno, pdf_size=pdf_size,
                       html_tables=html_tables, files_total=len(parsed),
                       file_ok=file_ok, file_fail=file_fail, dfile=dfile_note))
    elif not do_download:
        out.append(rec(target["apba_id"], name, item_name, rn, "report", "SUCCESS",
                       step="enum_only", dno=dno, files_total=len(parsed),
                       html_tables=html_tables))
    else:
        # disclosureNo는 있는데 pdf·file·html 전부 비었음 → 조사 필요
        out.append(rec(target["apba_id"], name, item_name, rn, "report",
                       "TOOL_ERROR", step="summary", dno=dno,
                       note="dno 있으나 pdf/file/html 모두 비었음",
                       files_total=len(parsed)))
    return out


def _try_dfile(sess, dno, sno, save_dir, out, target, item_name, rn, name):
    """itemReportFiles.json으로 dfile 파일명 발견 → download_disclosure_attachment(dfile).

    ※ 현재 MCP 도구에는 itemReportFiles.json(orcpFileNa)을 노출하는 도구가 없어
       파일명 발견 단계는 하네스가 직접 한다. 이 사실 자체가 커버리지 갭."""
    if not sno:
        return "sno 없음"
    try:
        fr = sess.get(f"{core.BASE_URL}/item/itemReportFiles.json?disclosureNo={dno}", timeout=15)
        files_data = fr.json().get("data", []) or []
    except Exception as e:
        out.append(rec(target["apba_id"], name, item_name, rn, "report",
                       "SERVER_DEFECT", step="itemReportFiles", dno=dno,
                       note=f"itemReportFiles 예외: {e}"))
        return "itemReportFiles 실패"
    if not files_data:
        return "dfile 목록 없음"
    fname = files_data[0].get("orcpFileNa", "")
    if not fname:
        return "orcpFileNa 없음"
    try:
        dr = M.download_disclosure_attachment("dfile", fileName=fname, submissionNo=sno, save_dir=save_dir)
        if isinstance(dr, dict) and "error" not in dr and dr.get("size_bytes", 0) > 0:
            return f"dfile OK {dr.get('size_bytes')}B"
        out.append(rec(target["apba_id"], name, item_name, rn, "report",
                       "SERVER_DEFECT", step="download_disclosure_attachment(dfile)",
                       dno=dno, sno=sno, fname=fname[:60],
                       tool_error=(dr or {}).get("error")))
        return "dfile 실패"
    except Exception as e:
        out.append(rec(target["apba_id"], name, item_name, rn, "report",
                       "CODE_EXCEPTION", step="download_disclosure_attachment(dfile)",
                       dno=dno, sno=sno, exc=f"{type(e).__name__}: {e}",
                       tb=traceback.format_exc()[-800:]))
        return "dfile 예외"


# ─────────────────────────────────────────────────────────
# 게시판형 1셀 처리 (MCP 도구 호출)
# ─────────────────────────────────────────────────────────
def collect_board_cell(sess, item, target, do_download):
    name = target["name"]
    item_name = core.build_item_display_name(item)
    rn = core.build_item_root_no(item)
    aid = target["apba_id"]
    out = []

    heavy = rn in HEAVY_BOARD_ROOTS
    step_name = "list_board_items" if heavy else "list_all_board_items"
    try:
        if heavy:
            res = M.list_board_items(rn, aid, page=1)  # 입찰공고: 1페이지만
        else:
            res = M.list_all_board_items(rn, aid)
    except Exception as e:
        out.append(rec(aid, name, item_name, rn, "board", "CODE_EXCEPTION",
                       step=step_name, exc=f"{type(e).__name__}: {e}",
                       tb=traceback.format_exc()[-800:]))
        return out

    if not isinstance(res, dict) or res.get("error"):
        err = (res or {}).get("error", "") if isinstance(res, dict) else "non-dict"
        # NOT_FOUND는 이 기관 미공시(정상)일 수 있음
        cls = "EMPTY_VALID" if "NOT_FOUND" in str(err) else "TOOL_ERROR"
        out.append(rec(aid, name, item_name, rn, "board", cls,
                       step=step_name, tool_error=err))
        return out

    items = res.get("자료", []) or []
    total = res.get("totalCnt", len(items))  # heavy(list_board_items)는 totalCnt 없음 → 페이지수
    if not items:
        out.append(rec(aid, name, item_name, rn, "board", "EMPTY_VALID",
                       step=step_name, note="자료 0"))
        return out

    att_total = 0
    att_ok = 0
    att_fail = 0
    extlink_only = 0
    records_with_attach = 0
    downloaded_records = 0
    inspected = items[:BOARD_MAX_RECORDS_INSPECT]  # 첨부 조사는 상한까지만

    for ridx, z in enumerate(inspected):
        meta = dict(
            apbaId=aid, reportFormNo=z.get("reportFormNo") or rn,
            idx=z.get("idx", ""), disclosureNo=z.get("공시번호", ""),
            tableName=z.get("tableName", ""), idxName=z.get("idxName", ""),
            bidType=z.get("bidType", ""),
        )
        try:
            la = M.list_board_attachments(**meta)
        except Exception as e:
            out.append(rec(aid, name, item_name, rn, "board", "CODE_EXCEPTION",
                           step="list_board_attachments", idx=z.get("idx"),
                           exc=f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-800:]))
            continue
        if not isinstance(la, dict):
            continue
        atts = la.get("첨부", []) or []
        exts = la.get("외부링크", []) or []
        if atts:
            records_with_attach += 1
            att_total += len(atts)
        elif exts:
            extlink_only += 1

        # 다운로드 상한: 첨부 있는 레코드 앞에서부터 N개만
        if do_download and atts and downloaded_records < BOARD_MAX_RECORDS_DOWNLOAD:
            downloaded_records += 1
            save_dir = _dl_dir(item_name, name)
            for a in atts[:BOARD_MAX_ATTACH_PER_RECORD]:
                try:
                    dr = M.download_board_attachment(
                        kind=a.get("kind", "upload"), name=a.get("name", ""),
                        spath=a.get("spath", ""), sfile=a.get("sfile", ""),
                        file_no=a.get("file_no", ""), save_dir=save_dir)
                    if isinstance(dr, dict) and "error" not in dr and dr.get("size_bytes", 0) > 0:
                        att_ok += 1
                    else:
                        att_fail += 1
                        emsg = (dr or {}).get("error", "")
                        # spath 연도누락 404·서버 결함 식별
                        is_defect = ("404" in str(emsg)) or ("HTTP 4" in str(emsg)) or ("HTTP 5" in str(emsg))
                        out.append(rec(aid, name, item_name, rn, "board",
                                       "SERVER_DEFECT" if is_defect else "TOOL_ERROR",
                                       step="download_board_attachment", idx=z.get("idx"),
                                       kind=a.get("kind"), fname=(a.get("name") or "")[:60],
                                       spath=a.get("spath", "")[:60], tool_error=emsg))
                except Exception as e:
                    att_fail += 1
                    out.append(rec(aid, name, item_name, rn, "board", "CODE_EXCEPTION",
                                   step="download_board_attachment", idx=z.get("idx"),
                                   kind=a.get("kind"), exc=f"{type(e).__name__}: {e}",
                                   tb=traceback.format_exc()[-800:]))

    # 셀 종합
    capped = (total > len(inspected)) or (do_download and records_with_attach > BOARD_MAX_RECORDS_DOWNLOAD)
    if att_ok > 0:
        cls = "SUCCESS"
    elif records_with_attach == 0 and extlink_only > 0:
        cls = "EXTLINK_ONLY"
    elif records_with_attach == 0:
        cls = "EMPTY_VALID"  # 자료는 있으나 첨부/외부링크 없음(목록형 정상)
    elif not do_download:
        cls = "SUCCESS"
    else:
        cls = "TOOL_ERROR"  # 첨부 있다는데 단 1건도 못 받음
    out.append(rec(aid, name, item_name, rn, "board", cls, step="summary",
                   total_records=total, inspected=len(inspected),
                   records_with_attach=records_with_attach,
                   att_total=att_total, att_ok=att_ok, att_fail=att_fail,
                   extlink_only=extlink_only, capped=capped))
    return out


# ─────────────────────────────────────────────────────────
# 내부규정 1셀 처리 (MCP 도구 호출)
# ─────────────────────────────────────────────────────────
def collect_rule_cell(sess, item, target, do_download):
    name = target["name"]
    item_name = core.build_item_display_name(item)
    rn = core.build_item_root_no(item)
    aid = target["apba_id"]
    out = []

    # 전체 건수(count_only) + 정관(K1500) 다운로드 메타
    try:
        cnt = M.list_rules(name, count_only=True)
    except Exception as e:
        out.append(rec(aid, name, item_name, rn, "rule", "CODE_EXCEPTION",
                       step="list_rules(count_only)", exc=f"{type(e).__name__}: {e}",
                       tb=traceback.format_exc()[-800:]))
        return out
    total = cnt.get("totalCnt", 0) if isinstance(cnt, dict) else 0
    if not total:
        out.append(rec(aid, name, item_name, rn, "rule", "EMPTY_VALID",
                       step="list_rules", note="내부규정 0건"))
        return out

    try:
        lst = M.list_rules(name, divis=RULE_DIVIS_TO_DOWNLOAD, include_files=True)
    except Exception as e:
        out.append(rec(aid, name, item_name, rn, "rule", "CODE_EXCEPTION",
                       step="list_rules(include_files)", exc=f"{type(e).__name__}: {e}",
                       tb=traceback.format_exc()[-800:]))
        return out
    if not isinstance(lst, dict) or lst.get("error"):
        out.append(rec(aid, name, item_name, rn, "rule", "EMPTY_VALID",
                       step="list_rules(K1500)", total_rules=total,
                       note=(lst or {}).get("error", "정관 없음")))
        return out
    rules = lst.get("규정", []) or []
    latest = next((r.get("latest") for r in rules if r.get("latest")), None)
    if not latest:
        out.append(rec(aid, name, item_name, rn, "rule", "EMPTY_VALID",
                       step="list_rules(K1500)", total_rules=total,
                       note="정관 파일 메타 없음"))
        return out

    if not do_download:
        out.append(rec(aid, name, item_name, rn, "rule", "SUCCESS", step="enum_only",
                       total_rules=total, file_no=latest.get("file_no")))
        return out

    save_dir = _dl_dir(item_name, name)
    try:
        dr = M.download_rule_file(latest.get("file_no"), latest.get("file_name", ""), save_dir=save_dir)
        if isinstance(dr, dict) and "error" not in dr and dr.get("size_bytes", 0) > 0:
            out.append(rec(aid, name, item_name, rn, "rule", "SUCCESS", step="download_rule_file",
                           total_rules=total, file_no=latest.get("file_no"),
                           size=dr.get("size_bytes")))
        else:
            out.append(rec(aid, name, item_name, rn, "rule", "TOOL_ERROR",
                           step="download_rule_file", total_rules=total,
                           file_no=latest.get("file_no"), tool_error=(dr or {}).get("error")))
    except Exception as e:
        out.append(rec(aid, name, item_name, rn, "rule", "CODE_EXCEPTION",
                       step="download_rule_file", file_no=latest.get("file_no"),
                       exc=f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-800:]))
    return out


# ─────────────────────────────────────────────────────────
# 실행 엔진
# ─────────────────────────────────────────────────────────
def run(targets, items, do_download=True, workers=8):
    report_items = [it for it in items if classify_route(it) == "report"]
    board_items = [it for it in items if classify_route(it) == "board"]
    rule_items = [it for it in items if classify_route(it) == "rule"]
    want_ids = {t["apba_id"] for t in targets}
    by_id = {t["apba_id"]: t for t in targets}

    results = []
    t0 = time.time()

    # ── 1) 보고서형 열거(항목당 1콜) — 병렬 ──
    enum_sess = _make_session()
    report_map = {}  # rootNo -> {apba_id: info}
    print(f"[1/4] 보고서형 {len(report_items)}개 항목 열거(itemOrganListJung)...", file=sys.stderr)

    def _enum(it):
        rn = core.build_item_root_no(it)
        return rn, enum_report_organs(_make_session(), rn, want_ids)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rn, m in ex.map(_enum, report_items):
            report_map[rn] = m
            if "__error__" in m:
                # 열거 자체 실패 → 이 항목 전 기관 TOOL_ERROR 1건으로
                results.append(rec("", "(전기관)", rn, rn, "report", "TOOL_ERROR",
                                   step="itemOrganListJung", tool_error=m["__error__"]))

    # ── 2) 보고서형 셀 처리 ──
    report_tasks = []
    for it in report_items:
        rn = core.build_item_root_no(it)
        m = report_map.get(rn, {})
        for aid in want_ids:
            report_tasks.append((it, by_id[aid], m.get(aid)))
    print(f"[2/4] 보고서형 셀 {len(report_tasks)}개 처리...", file=sys.stderr)

    def _do_report(args):
        it, tgt, info = args
        return collect_report_cell(_make_session(), it, tgt, info, do_download)

    _run_pool(_do_report, report_tasks, results, workers, "report")

    # ── 3) 게시판형 셀 처리 ──
    board_tasks = [(it, by_id[aid]) for it in board_items for aid in want_ids]
    print(f"[3/4] 게시판형 셀 {len(board_tasks)}개 처리...", file=sys.stderr)

    def _do_board(args):
        it, tgt = args
        return collect_board_cell(_make_session(), it, tgt, do_download)

    _run_pool(_do_board, board_tasks, results, workers, "board")

    # ── 4) 내부규정 셀 처리 ──
    rule_tasks = [(it, by_id[aid]) for it in rule_items for aid in want_ids]
    print(f"[4/4] 내부규정 셀 {len(rule_tasks)}개 처리...", file=sys.stderr)

    def _do_rule(args):
        it, tgt = args
        return collect_rule_cell(_make_session(), it, tgt, do_download)

    _run_pool(_do_rule, rule_tasks, results, workers, "rule")

    print(f"\n총 {len(results)}건 레코드, {time.time()-t0:.0f}초 소요", file=sys.stderr)
    return results


def _run_pool(fn, tasks, results, workers, label):
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fn, t) for t in tasks]
        for fut in as_completed(futs):
            try:
                results.extend(fut.result())
            except Exception as e:
                results.append({"route": label, "cls": "CODE_EXCEPTION",
                                "step": "pool", "exc": f"{type(e).__name__}: {e}",
                                "tb": traceback.format_exc()[-800:]})
            done += 1
            if done % 50 == 0:
                print(f"    [{label}] {done}/{len(tasks)}", file=sys.stderr)


# ─────────────────────────────────────────────────────────
# 리포트 출력
# ─────────────────────────────────────────────────────────
def write_outputs(results, args):
    os.makedirs(SAVE_ROOT, exist_ok=True)
    jsonl = os.path.join(HERE, f"stress_results_{STAMP}.jsonl")
    with open(jsonl, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 실패만 따로(조사 우선순위)
    fails = [r for r in results if r["cls"] in ("CODE_EXCEPTION", "TOOL_ERROR", "SERVER_DEFECT")]
    failpath = os.path.join(HERE, f"stress_failures_{STAMP}.json")
    with open(failpath, "w", encoding="utf-8") as f:
        json.dump(fails, f, ensure_ascii=False, indent=2)

    # 통계
    from collections import Counter
    cls_count = Counter(r["cls"] for r in results)
    print("\n" + "=" * 70)
    print(f"[스트레스 테스트 결과]  총 {len(results)}건 레코드")
    print("=" * 70)
    for c, n in cls_count.most_common():
        print(f"  {c:16s} {n}")

    # 라우트×분류 교차
    print("\n[라우트 × 분류]")
    rc = Counter((r["route"], r["cls"]) for r in results)
    for (rt, c), n in sorted(rc.items()):
        print(f"  {rt:8s} {c:16s} {n}")

    # CODE_EXCEPTION 군집(=우리 버그) 상위
    exc = [r for r in results if r["cls"] == "CODE_EXCEPTION"]
    print(f"\n[CODE_EXCEPTION {len(exc)}건 — 예외 유형별]")
    et = Counter((r.get("step", ""), (r.get("exc", "") or "").split(":")[0]) for r in exc)
    for (step, etype), n in et.most_common(20):
        print(f"  {n:4d}× step={step} exc={etype}")

    # TOOL_ERROR 군집
    te = [r for r in results if r["cls"] == "TOOL_ERROR"]
    print(f"\n[TOOL_ERROR {len(te)}건 — step·메시지별]")
    tt = Counter((r.get("step", ""), str(r.get("tool_error") or r.get("note") or "")[:50]) for r in te)
    for (step, msg), n in tt.most_common(25):
        print(f"  {n:4d}× step={step} :: {msg}")

    # SERVER_DEFECT 군집
    sd = [r for r in results if r["cls"] == "SERVER_DEFECT"]
    sc = Counter((r.get("step", ""), str(r.get("tool_error") or r.get("note") or "")[:40]) for r in sd)
    print(f"\n[SERVER_DEFECT {len(sd)}건 — step별]")
    for (step, msg), n in sc.most_common(15):
        print(f"  {n:4d}× step={step} :: {msg}")

    print(f"\n결과: {jsonl}")
    print(f"실패: {failpath}  ({len(fails)}건)")
    print(f"수집: {SAVE_ROOT}")
    return jsonl, failpath, cls_count


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--items", default="", help="콤마구분 rootNo/항목키워드 필터")
    p.add_argument("--limit-orgs", type=int, default=0, help="앞 N개 기관만")
    p.add_argument("--no-download", action="store_true")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--org-filter", default="위탁집행")
    args = p.parse_args()

    targets = load_targets(args.org_filter, args.limit_orgs)
    items = core.get_alio_items()
    if args.items:
        kws = [x.strip() for x in args.items.split(",") if x.strip()]
        items = [it for it in items
                 if core.build_item_root_no(it) in kws
                 or it.get("mcd", "") in kws
                 or any(k in core.build_item_display_name(it) for k in kws)]

    print(f"대상 기관: {len(targets)}개 ({args.org_filter})  |  항목: {len(items)}개  |  "
          f"다운로드: {not args.no_download}", file=sys.stderr)
    results = run(targets, items, do_download=not args.no_download, workers=args.workers)
    write_outputs(results, args)


if __name__ == "__main__":
    main()
