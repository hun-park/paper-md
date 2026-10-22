#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "==3.12.*"
# dependencies = ["docling==2.129.0", "pymupdf==1.27.2.3", "torch==2.11.0", "torchvision==0.26.0", "rapidocr==3.9.2"]
#
# [tool.uv]
# exclude-newer = "2026-10-22T00:00:00Z"
#
# [[tool.uv.index]]
# name = "pytorch-cu128"
# url = "https://download.pytorch.org/whl/cu128"
# explicit = true
#
# [tool.uv.sources]
# torch = { index = "pytorch-cu128" }
# torchvision = { index = "pytorch-cu128" }
# ///
"""
Spec: PDF to one markdown cache file. docling gives the structure, PyMuPDF restores the text of
formulas docling leaves undecoded (matched by page position) and fixes ligatures, hyphenation and
unreadable glyphs. Scanned PDFs have no text layer, so they use RapidOCR and docling formula recognition.
Goal: a deterministic file whose unresolved spots are reported in the side-car, never guessed.
"""
import argparse, collections, os, re, sys, tempfile

UNDECODED = "<!-- formula-not-decoded -->"
MARK = "⟦?⟧"
SCANNED_CHARS_PER_PAGE = 100
BAR = "=" * 70
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
SPACES = {"\u2011": "-", "\u00ad": "", "\u00a0": " ", "\u2002": " ", "\u2009": " ", "\u200a": " ", "\u202f": " "}
UNREADABLE = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\ufffd\ue000-\uf8ff]|\(cid:\d+\)")
SOFT_HYPHEN = re.compile(r"(?<=[a-z])- *\n(?=[a-z])")
BRACKET_PIECES = dict(zip(range(0xF8EB, 0xF8FF), "⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪⎮⎞⎟⎠⎤⎥⎦⎫⎬⎭"))
FONT_GLYPHS = {"CMEX10": {0x00: "(", 0x01: ")", 0x02: "[", 0x03: "]", 0x04: "⌊", 0x05: "⌋", 0x06: "⌈", 0x07: "⌉",
                          0x08: "{", 0x09: "}", 0x0A: "⟨", 0x0B: "⟩", 0x0C: "|", 0x0D: "‖", 0x0E: "/", 0x0F: "\\",
                          0x10: "(", 0x11: ")", 0x12: "(", 0x13: ")", 0x14: "[", 0x15: "]", 0x1A: "{", 0x1B: "}"}}


def font_name(span):
    return span["font"].split("+")[-1]


def span_text(span, glyph_map):
    font = font_name(span)
    table = glyph_map.get(font, {})
    return "".join(table.get(ord(ch), FONT_GLYPHS.get(font, {}).get(ord(ch), BRACKET_PIECES.get(ord(ch), ch)))
                   for ch in span["text"])


def line_texts(page, glyph_map, clip=None):
    for block in page.get_text("dict", clip=clip, sort=True)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span_text(span, glyph_map) for span in line["spans"])
            if text.strip():
                yield text


def clean(text, stats):
    for pairs, key in ((LIGATURES, "ligature"), (SPACES, "unicode")):
        for old, new in pairs.items():
            stats[key] += text.count(old)
            text = text.replace(old, new)
    stats["hyphen"] += len(SOFT_HYPHEN.findall(text))
    text = SOFT_HYPHEN.sub("", text)
    stats["mark"] += len(UNREADABLE.findall(text))
    return UNREADABLE.sub(MARK, text)


def undecoded_formulas(document):
    from docling_core.types.doc import DocItemLabel

    return [item for item, _ in document.iterate_items()
            if getattr(item, "label", None) == DocItemLabel.FORMULA and not item.text]


def formula_text(pdf_doc, item, glyph_map):
    import fitz

    prov = item.prov[0]
    page = pdf_doc[prov.page_no - 1]
    left, top, right, bottom = prov.bbox.to_top_left_origin(page.rect.height).as_tuple()
    clip = fitz.Rect(left - 2, top - 2, right + 2, bottom + 2)
    return " ".join(clean(" ".join(line_texts(page, glyph_map, clip)), collections.Counter()).split())


def fill_formulas(body, recovered):
    parts = body.split(UNDECODED)
    if len(parts) - 1 != len(recovered):
        return body, 0
    filled = parts[0]
    for text, tail in zip(recovered, parts[1:]):
        filled += (text or UNDECODED) + tail
    return filled, sum(bool(text) for text in recovered)


def select_pages(pdf, pages, workdir):
    import fitz

    pdf_doc = fitz.open(pdf)
    if not pages:
        return pdf_doc, pdf, 1
    first, last = (int(x) for x in pages.split("-"))
    pdf_doc.select(range(first - 1, last))
    path = os.path.join(workdir, "selection.pdf")
    pdf_doc.save(path)
    return pdf_doc, path, first


def is_scanned(pdf_doc):
    return sum(len(page.get_text()) for page in pdf_doc) / pdf_doc.page_count < SCANNED_CHARS_PER_PAGE


def pick_device():
    import torch, warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return "cuda" if torch.cuda.is_available() else "cpu"


def docling_document(path, scanned, device):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    ocr = RapidOcrOptions(backend="torch", force_full_page_ocr=True, rapidocr_params={"Global.log_level": "warning"})
    options = PdfPipelineOptions(accelerator_options=AcceleratorOptions(device=device), ocr_options=ocr,
                                 do_ocr=scanned, do_formula_enrichment=scanned)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    return converter.convert(path).document


def side_car(body, formulas_found, formulas_filled):
    notes = []
    if formulas_found and not formulas_filled:
        notes.append(f"[수식] {formulas_found}개 formula-not-decoded 미복원 (쪽 위치로 복원하지 못함)")
    elif body.count(UNDECODED):
        notes.append(f"[수식] {body.count(UNDECODED)}개 formula-not-decoded 남음 (해당 영역에 PyMuPDF 텍스트 없음)")
    notes += [f"[{MARK}] …{body[max(0, m.start() - 40):m.end() + 40]}…".replace("\n", " ")
              for m in re.finditer(re.escape(MARK), body)]
    return notes


def convert(pdf, pages, glyph_map):
    with tempfile.TemporaryDirectory() as workdir:
        pdf_doc, path, first = select_pages(pdf, pages, workdir)
        scanned, device = is_scanned(pdf_doc), pick_device()
        print(f"[device] {device}\n[ocr] {'on' if scanned else 'off'}", file=sys.stderr)
        document = docling_document(path, scanned, device)

    items = undecoded_formulas(document)
    body, filled = fill_formulas(document.export_to_markdown(), [formula_text(pdf_doc, i, glyph_map) for i in items])
    stats = collections.Counter(formulas_found=len(items), formulas_filled=filled, scanned=scanned)
    body = clean(body, stats)
    dump = "\n".join(f"<<<p{first + n}>>>\n" + clean("\n".join(line_texts(page, glyph_map)), collections.Counter())
                     for n, page in enumerate(pdf_doc))
    return body, stats, side_car(body, len(items), filled), dump


def header(cite, pages, stats):
    scope = f"p.{pages} 발췌" if pages else "full text extraction"
    steps = [f"docling 구조 추출{f' (p.{pages})' if pages else ''}",
             "RapidOCR·docling 수식 인식" if stats["scanned"] else "",
             f"수식 {stats['formulas_filled']}/{stats['formulas_found']}개 PyMuPDF 복원" if stats["formulas_found"] else "",
             f"하이픈 병합 {stats['hyphen']}" if stats["hyphen"] else "",
             f"리가처 {stats['ligature']}" if stats["ligature"] else "",
             f"특수 공백·하이픈 정규화 {stats['unicode']}" if stats["unicode"] else "",
             f"판독 불가 기호 {MARK} {stats['mark']}곳" if stats["mark"] else ""]
    return f"{cite} -- {scope} (개인 연구 참조용).\n조판 정리: {', '.join(s for s in steps if s)}.\n{BAR}\n\n"


def scan(pdf):
    import fitz

    spans = [(font_name(sp), sp["text"], "".join(x["text"] for x in ln["spans"]))
             for p in fitz.open(pdf) for b in p.get_text("dict")["blocks"] for ln in b.get("lines", []) for sp in ln["spans"]]
    odd = lambda ch: ord(ch) < 32 or 0xE000 <= ord(ch) <= 0xF8FF or ch == "�"
    suspect = {f for f, t, _ in spans if any(odd(ch) for ch in t)} - set(FONT_GLYPHS)
    count, example = collections.Counter(), {}
    for font, text, line in spans:
        for ch in text:
            latin = 0x80 <= ord(ch) <= 0xFF or 0xFB00 <= ord(ch) <= 0xFB06
            hit = odd(ch) or (font in suspect and (latin or (ord(ch) < 127 and not ch.isalnum() and not ch.isspace())))
            if hit and ord(ch) not in BRACKET_PIECES and ord(ch) not in FONT_GLYPHS.get(font, {}):
                count[(font, ord(ch))] += 1
                example.setdefault((font, ord(ch)), line.strip()[:70])
    for (font, code), n in count.most_common():
        print(f"{n:5d}  {font}:0x{code:02X}  {example[(font, code)]!r}")


def crop(pdf, spec, out):
    import fitz

    font, code, *nth = spec.split(":")
    target, wanted, seen = chr(int(code, 16)), int(nth[0]) if nth else 0, 0
    for page in fitz.open(pdf):
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in (s for s in line["spans"] if font_name(s) == font):
                    for char in (c for c in span["chars"] if c["c"] == target):
                        if seen == wanted:
                            r = fitz.Rect(char["bbox"])
                            page.get_pixmap(dpi=200, clip=fitz.Rect(r.x0 - 40, r.y0 - 14, r.x1 + 40, r.y1 + 14)).save(out)
                            return print(f"{out}  p{page.number + 1}")
                        seen += 1
    sys.exit(f"{spec} 없음")


def parse_glyphs(specs):
    glyph_map = collections.defaultdict(dict)
    for spec in specs:
        font_code, char = spec.split("=", 1)
        font, code = font_code.rsplit(":", 1)
        glyph_map[font][int(code, 16)] = char
    return dict(glyph_map)


def selftest():
    stats = collections.Counter()
    assert clean("com-\nplex ﬁt a\x1ft �t (cid:31)t li‑ion", stats) == f"complex fit a{MARK}t {MARK}t {MARK}t li-ion"
    assert (stats["hyphen"], stats["ligature"], stats["mark"], stats["unicode"]) == (1, 1, 3, 1)
    assert clean("SOC-\nOCV", collections.Counter()) == "SOC-\nOCV"
    assert clean("Fara- \nday", collections.Counter()) == "Faraday"
    span = {"font": "ABCDEF+AdvP4C4E74", "text": "SOCð0Þ ¼ a\x05b\uf8ee"}
    assert span_text(span, {"AdvP4C4E74": {0xF0: "(", 0xDE: ")", 0xBC: "=", 0x05: "−"}}) == "SOC(0) = a−b⎡"
    assert span_text({"font": "CMEX10", "text": "\x00x\x01"}, {}) == "(x)"
    assert fill_formulas(f"a {UNDECODED} b {UNDECODED} c", ["x=1", "y=2"]) == ("a x=1 b y=2 c", 2)
    assert fill_formulas(f"a {UNDECODED} b {UNDECODED} c", ["x=1", ""]) == (f"a x=1 b {UNDECODED} c", 1)
    assert fill_formulas(f"a {UNDECODED} b", ["x=1", "y=2"]) == (f"a {UNDECODED} b", 0)
    print("selftest ok")


def main():
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        return selftest()
    if argv[:1] == ["--scan"]:
        return scan(argv[1])
    if argv[:1] == ["--crop"]:
        return crop(*argv[1:4])
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("out")
    parser.add_argument("--cite", required=True)
    parser.add_argument("--pages")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--glyph", action="append", default=[], metavar="FONT:0xHH=CHAR")
    parser.add_argument("--fitz-dump")
    args = parser.parse_args()
    if os.path.exists(args.out) and not args.force:
        sys.exit(f"{args.out} 이미 있음 — REFS는 gitignored라 덮으면 복구 불가. 백업 후 --force")
    body, stats, notes, dump = convert(args.pdf, args.pages, parse_glyphs(args.glyph))
    if args.fitz_dump:
        with open(args.fitz_dump, "w", encoding="utf-8") as f:
            f.write(dump)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(header(args.cite, args.pages, stats) + body)
    print(f"wrote {args.out}: {len(body.splitlines())} lines, "
          f"formulas={stats['formulas_filled']}/{stats['formulas_found']}, marks={stats['mark']}")
    print("\n".join(notes) if notes else "(사이드카 없음)")


if __name__ == "__main__":
    main()
