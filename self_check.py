"""alio-mcp v0.4.1 자체점검 스크립트.

12개 MCP 도구를 라이브 호출해 응답·다운로드 헤더까지 검증한다.
크롤러의 self_check_v5_4_1.py와 같은 형식.

실행: python3 self_check.py
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime

import alio_mcp as m

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
PASS = f"{GREEN}[✓] PASS{RESET}"
FAIL = f"{RED}[✗] FAIL{RESET}"
SKIP = f"{YELLOW}[—] SKIP{RESET}"

results = {"pass": 0, "fail": 0, "skip": 0, "failures": []}


def check(label, cond, detail=""):
    if cond:
        print(f"  {PASS} {label}")
        if detail:
            print(f"           └ {detail}")
        results["pass"] += 1
    else:
        print(f"  {FAIL} {label}")
        if detail:
            print(f"           └ {detail}")
        results["fail"] += 1
        results["failures"].append((label, detail))


def skip(label, reason):
    print(f"  {SKIP} {label}")
    print(f"           └ {reason}")
    results["skip"] += 1


def main():
    print("=" * 70)
    print(f"alio-mcp v0.9.0 자체점검 ({datetime.now():%Y-%m-%d %H:%M:%S})")
    print("테스트 기관: 한국산업단지공단 (apbaId=C0208)")
    print("=" * 70)

    tmpdir = tempfile.mkdtemp(prefix="alio_mcp_selfcheck_")
    apba_id = "C0208"
    inst_name = "한국산업단지공단"

    # ───── [1] 메뉴·기관·검색 ─────
    print("\n[1] 메뉴·기관·검색")

    menus = m.list_menus("")
    check("list_menus(전체) — 92개",
          isinstance(menus, list) and len(menus) >= 90,
          f"{len(menus)}개")

    menus_op = m.list_menus("기관운영")
    check("list_menus('기관운영')",
          isinstance(menus_op, list) and len(menus_op) > 10,
          f"{len(menus_op)}개")

    organs = m.list_organs("10105", page=1)
    check("list_organs('10105' 일반현황) 1페이지",
          isinstance(organs, dict) and organs.get("totalCnt", 0) > 100,
          f"totalCnt={organs.get('totalCnt')}, 첫페이지={len(organs.get('기관', []))}건")

    s = m.search_organs("산업단지공단")
    check("search_organs('산업단지공단')",
          s.get("총_검색결과", 0) >= 1 and any(o["기관ID"] == apba_id for o in s.get("기관", [])),
          f"{s.get('총_검색결과')}건")

    f = m.search_organs(region="대구", org_type="위탁집행")
    check("search_organs(region=대구, org_type=위탁집행) — 신규 필터",
          f.get("총_검색결과", 0) >= 1
          and all("대구" in o["지역"] and "위탁집행" in o["기관유형"]
                  for o in f.get("기관", [])),
          f"{f.get('총_검색결과')}건 (전부 대구·위탁집행형)")

    organs_j = m.list_organs("21201", page=1)
    disc_j = (organs_j.get("기관") or [{}])[0].get("공시번호", "")
    rd = m.get_report_data(disc_j)
    check("get_report_data('21201' 징계현황 본문)",
          isinstance(rd, dict) and rd.get("표_개수", 0) > 0
          and "징계" in (rd.get("본문텍스트") or ""),
          f"표 {rd.get('표_개수')}개, 제목={(rd.get('제목') or '')[:24]}")

    # ───── [2] 게시판형 자료·첨부·다운로드 ─────
    print("\n[2] 게시판형 자료·첨부·다운로드")

    bi = m.list_board_items("B1220", apba_id, page=1)
    check("list_board_items('B1220') 1페이지",
          isinstance(bi, dict) and len(bi.get("자료", [])) > 0,
          f"{len(bi.get('자료', []))}건")

    bi_all = m.list_all_board_items("B1220", apba_id)
    check("list_all_board_items('B1220') 전체",
          bi_all.get("totalCnt", 0) >= len(bi.get("자료", [])),
          f"{bi_all.get('totalCnt')}건")

    audit_all = m.list_all_board_items("43006", apba_id)
    check("list_all_board_items('43006' 자체감사)",
          audit_all.get("totalCnt", 0) > 20,
          f"{audit_all.get('totalCnt')}건")

    if bi.get("자료"):
        top = bi["자료"][0]
        ba = m.list_board_attachments(
            apbaId=apba_id, reportFormNo=top["reportFormNo"],
            idx=top["idx"] or "", disclosureNo=top["공시번호"] or "",
            tableName=top["tableName"] or "", idxName=top["idxName"] or "",
            bidType=top["bidType"] or "",
        )
        check("list_board_attachments (첫 자료)",
              len(ba.get("첨부", [])) > 0,
              f"첨부 {len(ba.get('첨부', []))}건")

        if ba.get("첨부"):
            first = ba["첨부"][0]
            r = m.download_board_attachment(
                kind=first["kind"], name=first["name"],
                spath=first["spath"], sfile=first["sfile"],
                file_no=first["file_no"], save_dir=tmpdir,
            )
            ok = r.get("size_bytes", 0) > 1024 and os.path.exists(r.get("saved_path", ""))
            head = b""
            if ok:
                with open(r["saved_path"], "rb") as f:
                    head = f.read(8)
            check("download_board_attachment (PDF 헤더)",
                  ok and head.startswith(b"%PDF"),
                  f"{r.get('size_bytes')} bytes, head={head[:4]!r}")
        else:
            skip("download_board_attachment", "첨부 없음")
    else:
        skip("list_board_attachments / download_board_attachment", "자료 없음")

    # ───── [3] 보고서 PDF 다운로드 ─────
    print("\n[3] 보고서 PDF 다운로드")

    # 산단공 일반현황(10105)에서 disclosureNo 추출
    o_first = organs.get("기관", [])
    target = next((o for o in o_first if o["기관ID"] == apba_id), None)
    if target and target.get("공시번호"):
        r = m.download_report(target["공시번호"], save_dir=tmpdir,
                              filename=f"report_{target['공시번호']}.pdf")
        ok = r.get("size_bytes", 0) > 1024 and os.path.exists(r.get("saved_path", ""))
        head = b""
        if ok:
            with open(r["saved_path"], "rb") as f:
                head = f.read(8)
        check("download_report (일반현황 PDF 헤더)",
              ok and head.startswith(b"%PDF"),
              f"{r.get('size_bytes')} bytes, head={head[:4]!r}")
    else:
        skip("download_report", "산단공 일반현황 disclosureNo 없음")

    # ───── [4] 내부규정 ─────
    print("\n[4] 내부규정 (rule 체인)")

    # v0.4.1 count_only 경량 모드 (HTTP 1회)
    count = m.list_rules(inst_name, count_only=True)
    check("list_rules(산단공, count_only=True)",
          count.get("totalCnt", 0) >= 1 and "규정" not in count,
          f"{count.get('totalCnt')}건, 분류={count.get('분류명')}, payload keys={list(count.keys())}")

    rules = m.list_rules(inst_name, divis="K1500", include_files=True)
    check("list_rules(산단공, K1500 정관)",
          rules.get("totalCnt", 0) >= 1 and rules.get("규정"),
          f"{rules.get('totalCnt')}건, 분류={rules.get('분류명')}")

    if rules.get("규정") and rules["규정"][0].get("latest"):
        latest = rules["규정"][0]["latest"]
        r = m.download_rule_file(fileNo=latest["file_no"],
                                 fileName=latest["file_name"], save_dir=tmpdir)
        ok = r.get("size_bytes", 0) > 1024 and os.path.exists(r.get("saved_path", ""))
        check("download_rule_file (정관 HWP)",
              ok,
              f"{r.get('size_bytes')} bytes, name={latest['file_name']}")
    else:
        skip("download_rule_file", "정관 최신 파일 메타 없음")

    # ───── [5] 보고서 부속 첨부 (list_disclosure_attachments → file/dfile) ─────
    print("\n[5] 보고서 부속 첨부 (v0.9.0 목록 도구 → file/dfile 실다운로드)")

    # 5-1) 손익계산서(31301) 등 부속 file: 목록 도구로 fileNo 확보 → kind='file'
    organs_pl = m.list_organs("31301,31303", page=1)
    target_pl = next((o for o in organs_pl.get("기관", []) if o.get("공시번호")), None)
    if target_pl:
        dno_pl = target_pl["공시번호"]
        att = m.list_disclosure_attachments(dno_pl)
        check("list_disclosure_attachments(31301 손익) — 신규 도구",
              isinstance(att, dict) and len(att.get("첨부", [])) > 0
              and all(a.get("fileNo") for a in att.get("첨부", [])),
              f"→ {len(att.get('첨부', []))}건, 첫 fileNo={att.get('첨부', [{}])[0].get('fileNo')}")
        if att.get("첨부"):
            a0 = att["첨부"][0]
            r = m.download_disclosure_attachment(
                kind="file", fileName=a0["fileName"], disclosureNo=dno_pl,
                fileId=a0["fileNo"], save_dir=tmpdir)
            ok = r.get("size_bytes", 0) > 1024 and os.path.exists(r.get("saved_path", ""))
            check("download_disclosure_attachment(file) — 목록도구 fileNo로 실다운로드",
                  ok, f"{r.get('size_bytes')} bytes, name={a0['fileName'][:30]}")
        else:
            skip("download_disclosure_attachment(file)", "부속 첨부 없음")
    else:
        skip("list_disclosure_attachments(31301)", "손익계산서 공시 기관 없음")

    # 5-2) 안전경영책임보고서(70401) dfile: 목록 도구의 fileName+submissionNo로 다운로드
    organs_safety = m.list_organs("70401", page=1)
    target_s = next((o for o in organs_safety.get("기관", []) if o.get("공시번호")), None)
    if target_s:
        att_s = m.list_disclosure_attachments(target_s["공시번호"])
        if att_s.get("첨부") and att_s["첨부"][0].get("submissionNo"):
            g = att_s["첨부"][0]
            r = m.download_disclosure_attachment(
                kind="dfile", fileName=g["fileName"],
                submissionNo=g["submissionNo"], save_dir=tmpdir)
            ok = r.get("size_bytes", 0) > 1024 and os.path.exists(r.get("saved_path", ""))
            check("download_disclosure_attachment(dfile) — 안전경영책임보고서 실다운로드",
                  ok, f"{r.get('size_bytes')} bytes, name={g['fileName'][:30]}")
        else:
            skip("download_disclosure_attachment(dfile)", "70401 부속 첨부 메타 없음")
    else:
        skip("download_disclosure_attachment(dfile)", "사망자수 공시 기관 없음")

    # ───── [6] v0.7.0 신규 (저장경로·include_files 기본·truncated) ─────
    print("\n[6] v0.7.0 신규 동작")

    dsd = m._default_save_dir()
    check("기본 저장경로 크로스플랫폼(/tmp 아님)",
          not dsd.startswith("/tmp") and dsd.endswith(os.path.join("Downloads", "alio")),
          f"→ {dsd}")

    light = m.list_rules(inst_name, divis="K1500")
    has_latest = bool(light.get("규정") and light["규정"][0].get("latest"))
    check("list_rules 기본 경량(include_files 생략 → latest 부재)",
          bool(light.get("규정")) and not has_latest,
          f"→ {light.get('totalCnt')}건, latest부재={not has_latest}")

    big = m.search_organs(org_type="기타공공기관")
    check("search_organs truncated 필드",
          "truncated" in big and "표시" in big,
          f"→ 총 {big.get('총_검색결과')}, truncated={big.get('truncated')}")

    # ───── [7] v0.8.0 신규 도구 4종 ─────
    print("\n[7] v0.8.0 신규 도구")

    prof = m.get_organ_profile(apbaId=apba_id)
    check("get_organ_profile(C0208)",
          prof.get("기관명") == inst_name and bool(prof.get("기관장")) and bool(prof.get("홈페이지")),
          f"→ 장={prof.get('기관장')}, HP={prof.get('홈페이지')}")

    tree = m.list_menus_tree()
    check("list_menus_tree 계층+파일유형",
          isinstance(tree, dict) and len(tree) >= 3
          and all("파일유형" in it for v in tree.values() for it in v[:1]),
          f"→ 대분류 {len(tree)}개")

    disc = m.get_structured_summary("discipline", apbaId=apba_id)
    check("get_structured_summary(discipline)",
          isinstance(disc.get("징계건수"), dict) and disc.get("총건수", -1) >= 0,
          f"→ 총 {disc.get('총건수')}건")

    integ = m.get_structured_summary("integrity", apbaId=apba_id)
    check("get_structured_summary(integrity)",
          isinstance(integ.get("연도별등급"), dict) and len(integ.get("연도", [])) > 0,
          f"→ 연도 {integ.get('연도')}")

    safe = m.get_structured_summary("safety", apbaId=apba_id)
    check("get_structured_summary(safety) 안내에러",
          str(safe.get("error", "")).startswith("UNSUPPORTED"),
          f"→ {str(safe.get('error', ''))[:30]}")

    cmp = m.compare_organs("21201", names=f"{inst_name},한국전력공사")
    check("compare_organs 2기관 병렬",
          cmp.get("비교기관수") == 2 and all(r.get("표_개수", 0) > 0 for r in cmp.get("결과", [])),
          f"→ {[r['기관명'] for r in cmp.get('결과', [])]}")

    # ───── 정리 ─────
    print()
    print("=" * 70)
    total = results["pass"] + results["fail"] + results["skip"]
    print(f"결과: PASS={results['pass']}, FAIL={results['fail']}, SKIP={results['skip']} / 총 {total}건")
    print("=" * 70)

    if results["failures"]:
        print("\n[실패 항목]")
        for label, detail in results["failures"]:
            print(f"  ✗ {label}: {detail}")

    shutil.rmtree(tmpdir, ignore_errors=True)
    sys.exit(0 if results["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
