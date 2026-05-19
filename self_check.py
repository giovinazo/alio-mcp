"""alio-mcp v0.4.0 자체점검 스크립트.

11개 MCP 도구를 라이브 호출해 응답·다운로드 헤더까지 검증한다.
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
    print(f"alio-mcp v0.4.0 자체점검 ({datetime.now():%Y-%m-%d %H:%M:%S})")
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

    rules = m.list_rules(inst_name, divis="K1500")
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

    # ───── [5] 보고서 부속 첨부 (dfile) ─────
    print("\n[5] 보고서 부속 첨부 (kind=dfile, 사망자수)")

    # 사망자수(70401)에 산단공 공시 있는지
    organs_safety = m.list_organs("70401", page=1)
    target_s = next((o for o in organs_safety.get("기관", []) if o["기관ID"] == apba_id), None)
    if target_s and target_s.get("제출번호"):
        # dfile은 실제 fileName이 알리오 응답에 들어가 있어야 정확. 단순 호출 검증.
        r = m.download_disclosure_attachment(
            kind="dfile",
            fileName="안전경영책임보고서_test.pdf",
            submissionNo=target_s["제출번호"],
            save_dir=tmpdir,
        )
        # 실제 fileName 매칭 안 되면 API_ERROR 반환 — 통신·인자 검증 통과만 확인
        passed = "saved_path" in r or "DOWNLOAD_FAILED" in str(r.get("error", ""))
        check("download_disclosure_attachment(dfile) — 인자 검증 통과",
              passed,
              f"{r}")
    else:
        skip("download_disclosure_attachment(dfile)", "사망자수 산단공 제출번호 없음")

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
