#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 폴더를 Windows 호환 zip으로 재압축.

macOS Finder '압축'의 3대 결함(UTF-8 플래그 미설정·NFD 자모분해·__MACOSX/.DS_Store)을
제거한다: 파일명을 NFC로 정규화하고 각 엔트리에 UTF-8(EFS, 0x800) 플래그를 세팅하며
macOS 메타데이터를 제외한다.

사용: python3 repack_zip_for_windows.py <소스폴더> <출력.zip>
"""
import sys
import os
import zipfile
import unicodedata
import time

SKIP_NAMES = {".DS_Store"}
SKIP_DIRS = {"__MACOSX"}


def main():
    src = sys.argv[1].rstrip("/")
    out = sys.argv[2]
    base = os.path.basename(src)
    n = 0
    total = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=1, allowZip64=True) as zf:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                       and not d.startswith("._")]
            for fn in files:
                if fn in SKIP_NAMES or fn.startswith("._"):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src)
                # 최상위 폴더 포함 + 경로 구분자 정규화 + NFC 정규화
                arc = unicodedata.normalize(
                    "NFC", (base + "/" + rel).replace(os.sep, "/"))
                try:
                    st = os.stat(full)
                    dt = time.localtime(st.st_mtime)[:6]
                except OSError:
                    dt = (1980, 1, 1, 0, 0, 0)
                zi = zipfile.ZipInfo(arc, date_time=dt)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.flag_bits |= 0x800              # UTF-8(EFS) 플래그
                zi.external_attr = 0o644 << 16
                with open(full, "rb") as f:
                    zf.writestr(zi, f.read())
                n += 1
                total += st.st_size if 'st' in dir() else 0
                if n % 200 == 0:
                    print(f"  {n} files...")
    print(f"완료: {n} files -> {out} ({os.path.getsize(out)//(1024*1024)} MB)")


if __name__ == "__main__":
    main()
