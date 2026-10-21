#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "==3.12.*"
# dependencies = ["docling==2.129.0", "pymupdf==1.27.2.3"]
#
# [tool.uv]
# exclude-newer = "2026-10-22T00:00:00Z"
# ///
"""REFS 캐시 생성 — PDF → md.  ① docling 본문 구조  ② pymupdf 수식·글리프 보완.

usage:
  paper_md.py <pdf> <out.md> --cite "..." [--pages 3-9] [--glyph FONT:0xHH=CHAR ...] [--fitz-dump PATH] [--force]
  paper_md.py --scan <pdf>                         # 글리프 판정: 의심 (폰트, 코드) 집계
  paper_md.py --crop <pdf> FONT:0xHH[:n] <out.png>  # 글리프 판정: 그 자리를 잘라 본다
  paper_md.py --selftest

pymupdf 보완(결정적인 것만):
  - docling이 <!-- formula-not-decoded -->로 남긴 수식을 pymupdf 텍스트에서 복원
  - 줄끝 하이픈 병합, 리가처 풀기, 판독 불가 기호 글리프(C0·U+FFFD·PUA)는 ⟦?⟧로 표시
판독이 필요한 것(수식 정리, ⟦?⟧ 복원)은 stdout 사이드카로 넘기고 사람이/Claude가 한다.
"""
import argparse, collections, os, re, statistics, sys, tempfile

LIG = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
UNI = {"‑": "-", "­": "", " ": " ", " ": " ", " ": " ", " ": " ", " ": " "}
CTRL = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f�-]|\(cid:\d+\)")
WORD = re.compile(r"[A-Za-z]+")
HYPH = re.compile(r"(?<=[a-z])- *\n(?=[a-z])")
EQ = re.compile(r"^\((\d{1,3})\)\s*\n|\((\d{1,3})\)\s*$")
MARK = "⟦?⟧"
PUA = dict(zip(range(0xF8EB, 0xF8FF), "⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪⎮⎞⎟⎠⎤⎥⎦⎫⎬⎭"))
FONT = {"CMEX10": {0x00: "(", 0x01: ")", 0x02: "[", 0x03: "]", 0x04: "⌊", 0x05: "⌋", 0x06: "⌈", 0x07: "⌉",
                   0x08: "{", 0x09: "}", 0x0A: "⟨", 0x0B: "⟩", 0x0C: "|", 0x0D: "‖", 0x0E: "/", 0x0F: "\\",
                   0x10: "(", 0x11: ")", 0x12: "(", 0x13: ")", 0x14: "[", 0x15: "]", 0x1A: "{", 0x1B: "}"}}
BAR = "=" * 70


def glyphs(sp, gmap, hits):
    f = sp["font"].split("+")[-1]
    m = gmap.get(f, {})
    out = []
    for ch in sp["text"]:
        c = ord(ch)
        r = m.get(c, FONT.get(f, {}).get(c, PUA.get(c)))
        if r is None:
            out.append(ch)
            continue
        out.append(r)
        if hits is not None:
            hits[f"{f} 0x{c:02X}→{r or '∅'}"] += 1
    return "".join(out)


def page_lines(page, gmap=None, hits=None):
    for b in page.get_text("dict", sort=True)["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(glyphs(sp, gmap or {}, hits) for sp in ln["spans"])
            if t.strip():
                yield t, ln["bbox"][1], ln["bbox"][3], abs(ln["dir"][0] - 1) > 1e-3


def plain(t):
    return clean(t, collections.Counter())


def clean(t, st):
    for a, b in LIG.items():
        st["ligature"] += t.count(a)
        t = t.replace(a, b)
    for a, b in UNI.items():
        st["unicode"] += t.count(a)
        t = t.replace(a, b)
    st["hyphen"] += len(HYPH.findall(t))
    t = HYPH.sub("", t)
    st["mark"] += len(CTRL.findall(t))
    return CTRL.sub(MARK, t)


def extract_equations_pymupdf(doc, gmap=None):
    """PyMuPDF에서 번호 붙은 수식과 주변 텍스트 추출 — (N) 패턴으로."""
    ghits = collections.Counter()
    eqs = {}
    for i, page in enumerate(doc):
        blocks = page.get_text("blocks", sort=True)
        for b in blocks:
            m = EQ.search(b[4])
            if m:
                num = int(m.group(1) or m.group(2))
                frag = [" ".join(plain(x[4]).split()) for x in blocks
                        if x[1] < b[3] and x[3] > b[1] and x[0] < b[2] and x[2] > b[0]]
                eqs[num] = " ".join(frag)
    return eqs, ghits


def convert(pdf, pages=None, gmap=None):
    import fitz
    from docling.document_converter import DocumentConverter

    doc = fitz.open(pdf)
    pdf_path = pdf
    first = 1
    if pages:
        a, b = (int(x) for x in pages.split("-"))
        doc.select(range(a - 1, b))
        first = a
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
        doc.save(tmp)
        pdf_path = tmp
    n = doc.page_count

    # ① docling 본문 추출
    converter = DocumentConverter()
    result = converter.convert(pdf_path)
    body = result.document.export_to_markdown()

    # ② PyMuPDF에서 번호 붙은 수식 추출
    eqs, ghits = extract_equations_pymupdf(doc, gmap)

    # ③ <!-- formula-not-decoded --> 자리표시자에 PyMuPDF 수식 삽입
    formula_count = body.count("<!-- formula-not-decoded -->")
    eq_nums = sorted(eqs.keys())
    eq_idx = 0
    replaced_formulas = 0

    def replace_formula(match):
        nonlocal eq_idx, replaced_formulas
        if eq_idx < len(eq_nums):
            num = eq_nums[eq_idx]
            eq_idx += 1
            replaced_formulas += 1
            return eqs[num]
        return match.group(0)

    body = re.sub(r"<!-- formula-not-decoded -->", replace_formula, body)

    # ④ 정리
    st = collections.Counter()
    body = clean(body, st)
    st["glyphs"] = ghits
    st["formulas_total"] = formula_count
    st["formulas_replaced"] = replaced_formulas

    # 사이드카
    side = []
    remaining = body.count("<!-- formula-not-decoded -->")
    if remaining:
        side.append(f"[수식] {remaining}개 미복원 formula-not-decoded 남음")
    for m_obj in re.finditer(re.escape(MARK), body):
        side.append(f"[{MARK}] …{body[max(0, m_obj.start() - 40):m_obj.end() + 40]}…".replace("\n", " "))

    dump = "\n".join(f"<<<p{first + i}>>>\n" + plain("\n".join(t for t, *_ in page_lines(p, gmap)))
                     for i, p in enumerate(doc))

    return body, st, side, dump


def header(cite, pages, st):
    scope = f"p.{pages} 발췌" if pages else "full text extraction"
    fix = [f"docling 구조 추출" if not pages else f"docling 구조 추출 (p.{pages})",
           f"수식 {st.get('formulas_replaced', 0)}/{st.get('formulas_total', 0)}개 PyMuPDF 복원" if st.get("formulas_total") else "",
           f"하이픈 병합 {st['hyphen']}" if st["hyphen"] else "",
           f"리가처 {st['ligature']}" if st["ligature"] else "",
           f"특수 공백·하이픈 정규화 {st['unicode']}" if st["unicode"] else "",
           f"판독 불가 기호 {MARK} {st['mark']}곳" if st["mark"] else ""]
    return (f"{cite} -- {scope} (개인 연구 참조용).\n"
            f"조판 정리: {', '.join(f for f in fix if f)}.\n{BAR}\n\n")


def scan(pdf):
    import fitz
    spans = [(sp["font"].split("+")[-1], sp["text"], "".join(x["text"] for x in ln["spans"]))
             for p in fitz.open(pdf) for b in p.get_text("dict")["blocks"] for ln in b.get("lines", []) for sp in ln["spans"]]
    odd = lambda ch: ord(ch) < 32 or 0xE000 <= ord(ch) <= 0xF8FF or ch == "�"
    sus = {f for f, t, _ in spans if any(odd(ch) for ch in t)} - set(FONT)
    c, ex = collections.Counter(), {}
    for f, t, line in spans:
        for ch in t:
            latin = 0x80 <= ord(ch) <= 0xFF or 0xFB00 <= ord(ch) <= 0xFB06
            hit = odd(ch) or (f in sus and (latin or (ord(ch) < 127 and not ch.isalnum() and not ch.isspace())))
            if hit and ord(ch) not in PUA and ord(ch) not in FONT.get(f, {}):
                c[(f, ord(ch))] += 1
                ex.setdefault((f, ord(ch)), line.strip()[:70])
    for (f, k), v in c.most_common():
        print(f"{v:5d}  {f}:0x{k:02X}  {ex[(f, k)]!r}")


def crop(pdf, spec, out):
    import fitz
    f, c, *k = spec.split(":")
    ch, nth, i = chr(int(c, 16)), int(k[0]) if k else 0, 0
    for p in fitz.open(pdf):
        for b in p.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    if sp["font"].split("+")[-1] != f:
                        continue
                    for x in sp["chars"]:
                        if x["c"] == ch:
                            if i == nth:
                                r = fitz.Rect(x["bbox"])
                                p.get_pixmap(dpi=200, clip=fitz.Rect(r.x0 - 40, r.y0 - 14, r.x1 + 40, r.y1 + 14)).save(out)
                                return print(f"{out}  p{p.number + 1}")
                            i += 1
    sys.exit(f"{spec} 없음")


def selftest():
    st = collections.Counter()
    assert clean("com-\nplex ﬁt a\x1ft �t (cid:31)t li‑ion", st) == f"complex fit a{MARK}t {MARK}t {MARK}t li-ion"
    assert (st["hyphen"], st["ligature"], st["mark"], st["unicode"]) == (1, 1, 3, 1)
    assert clean("SOC-\nOCV", collections.Counter()) == "SOC-\nOCV"
    assert clean("Fara- \nday", collections.Counter()) == "Faraday"
    hits = collections.Counter()
    sp = {"font": "ABCDEF+AdvP4C4E74", "text": "SOCð0Þ ¼ a\x05b"}
    assert glyphs(sp, {"AdvP4C4E74": {0xF0: "(", 0xDE: ")", 0xBC: "=", 0x05: "−"}}, hits) == "SOC(0) = a−b⎡"
    assert glyphs({"font": "CMEX10", "text": "\x00x\x01"}, {}, None) == "(x)"
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
        sys.exit()
    if sys.argv[1:2] == ["--scan"]:
        scan(sys.argv[2])
        sys.exit()
    if sys.argv[1:2] == ["--crop"]:
        crop(*sys.argv[2:5])
        sys.exit()
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("out")
    ap.add_argument("--cite", required=True)
    ap.add_argument("--pages")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--glyph", action="append", default=[], metavar="FONT:0xHH=CHAR")
    ap.add_argument("--fitz-dump")
    a = ap.parse_args()
    if os.path.exists(a.out) and not a.force:
        sys.exit(f"{a.out} 이미 있음 — REFS는 gitignored라 덮으면 복구 불가. 백업 후 --force")
    gmap = collections.defaultdict(dict)
    for g in a.glyph:
        fc, ch = g.split("=", 1)
        f, c = fc.rsplit(":", 1)
        gmap[f][int(c, 16)] = ch
    text, st, side, dump = convert(a.pdf, a.pages, dict(gmap))
    if a.fitz_dump:
        with open(a.fitz_dump, "w", encoding="utf-8") as f:
            f.write(dump)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(header(a.cite, a.pages, st) + text)
    print(f"wrote {a.out}: {len(text.splitlines())} lines, "
          f"formulas={st.get('formulas_replaced', 0)}/{st.get('formulas_total', 0)}, "
          f"marks={st['mark']}")
    print("\n".join(side) if side else "(사이드카 없음)")
