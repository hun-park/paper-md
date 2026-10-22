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
Spec: PDF to one markdown cache file. docling gives the structure and recognizes formulas as LaTeX;
PyMuPDF supplies equation numbers, replaces formulas the recognizer got visibly wrong, and fixes ligatures,
hyphenation and unreadable glyphs. PDFs with almost no text layer also use RapidOCR.
Goal: a deterministic file whose unresolved spots are reported in the side-car, never guessed.
"""
import argparse, collections, logging, os, re, sys, tempfile, warnings

UNDECODED = "<!-- formula-not-decoded -->"
MARK = "⟦?⟧"
SPARSE_CHARS_PER_PAGE = 100
BAR = "=" * 70
SAMPLE_LINES = ["The battery terminal voltage depends on the internal resistance of the cell.",
                "Charging current raises the state of charge while the voltage relaxes slowly.",
                "Parameters are identified from the measured response to current pulses."]
SAMPLE_WORDS = ["resistance", "charging", "relaxes", "identified", "pulses"]
EQUATION_NUMBER = re.compile(r"\((\d{1,3})\)\s*$")
LATEX_TAIL = re.compile(r"(?:\s*(?:\\\\|&))+\s*$")
EMPTY_ROW = re.compile(r"\\\\\s*&\s*\\\\.*", re.S)
ENVIRONMENT = re.compile(r"\\begin\{(\w+)\}.*?\\end\{\1\}", re.S)
LATEX_RUN = re.compile(r"(\S+)(?: \1){11,}")
MAX_LATEX_TO_LAYER = 8
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
SPACES = {"\u2011": "-", "\u00ad": "", "\u00a0": " ", "\u2002": " ", "\u2009": " ", "\u200a": " ", "\u202f": " "}
UNREADABLE = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\ufffd\ue000-\uf8ff]|\(cid:\d+\)")
SOFT_HYPHEN = re.compile(r"(?<=[a-z])- *\n(?=[a-z])")
BRACKET_PIECES = dict(zip(range(0xF8EB, 0xF8FF), "⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪⎮⎞⎟⎠⎤⎥⎦⎫⎬⎭"))
FONT_GLYPHS = {"CMEX10": {0x00: "(", 0x01: ")", 0x02: "[", 0x03: "]", 0x04: "⌊", 0x05: "⌋", 0x06: "⌈", 0x07: "⌉",
                          0x08: "{", 0x09: "}", 0x0A: "⟨", 0x0B: "⟩", 0x0C: "|", 0x0D: "‖", 0x0E: "/", 0x0F: "\\",
                          0x10: "(", 0x11: ")", 0x12: "(", 0x13: ")", 0x14: "[", 0x15: "]", 0x1A: "{", 0x1B: "}"}}


def silence_known_noise():
    os.environ.update(TQDM_DISABLE="1", TRANSFORMERS_VERBOSITY="error", HF_HUB_DISABLE_PROGRESS_BARS="1")
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    warnings.filterwarnings("ignore", message="Using padding='same'", category=UserWarning)
    logging.getLogger("docling.datamodel.stage_model_specs").addFilter(lambda r: "already registered" not in r.getMessage())


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


def formula_items(document):
    from docling_core.types.doc import DocItemLabel

    return [item for item, _ in document.iterate_items() if getattr(item, "label", None) == DocItemLabel.FORMULA]


def formula_text(pdf_doc, item, glyph_map):
    import fitz

    prov = item.prov[0]
    page = pdf_doc[prov.page_no - 1]
    left, top, right, bottom = prov.bbox.to_top_left_origin(page.rect.height).as_tuple()
    clip = fitz.Rect(left - 2, top - 2, right + 2, bottom + 2)
    return " ".join(" ".join(line_texts(page, glyph_map, clip)).split())


def tidy_latex(latex):
    cut = EMPTY_ROW.search(latex)
    if cut and latex[:cut.start()].count("\\begin") == latex[:cut.start()].count("\\end"):
        latex = latex[:cut.start()]
    latex = LATEX_TAIL.sub("", latex).strip()
    outside = ENVIRONMENT.sub("", latex)
    return f"\\begin{{aligned}} {latex} \\end{{aligned}}" if "&" in outside or "\\\\" in outside else latex


def is_degenerate(latex, layer):
    return bool(LATEX_RUN.search(latex)) or (bool(layer) and len(latex) > MAX_LATEX_TO_LAYER * len(layer))


def refine_formulas(document, pdf_doc, glyph_map):
    items = formula_items(document)
    layers = [formula_text(pdf_doc, item, glyph_map) for item in items]
    for item, layer in zip(items, layers):
        latex = tidy_latex(item.text)
        number = EQUATION_NUMBER.search(layer)
        item.text = "" if is_degenerate(latex, layer) else f"{latex} \\tag{{{number.group(1)}}}" if latex and number else latex
    return [layer for item, layer in zip(items, layers) if not item.text], len(items)


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
    match = re.fullmatch(r"(\d+)-(\d+)", pages)
    first, last = (int(x) for x in match.groups()) if match else (0, 0)
    if not 1 <= first <= last <= pdf_doc.page_count:
        sys.exit(f"--pages는 a-b 형식이고 1 ≤ a ≤ b ≤ {pdf_doc.page_count}쪽이어야 한다: {pages}")
    pdf_doc.select(range(first - 1, last))
    path = os.path.join(workdir, "selection.pdf")
    pdf_doc.save(path)
    return pdf_doc, path, first


def sparse_pages(pdf_doc):
    return [n for n, page in enumerate(pdf_doc) if len(page.get_text()) < SPARSE_CHARS_PER_PAGE]


def needs_ocr(pdf_doc):
    thin = len(sparse_pages(pdf_doc))
    return thin * 2 >= pdf_doc.page_count, f"글자 {SPARSE_CHARS_PER_PAGE}자 미만 쪽 {thin}/{pdf_doc.page_count}"


def pick_device():
    import torch

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return "cuda" if torch.cuda.is_available() else "cpu"


def docling_converter(device, ocr):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    rapid = RapidOcrOptions(backend="torch", force_full_page_ocr=True, rapidocr_params={"Global.log_level": "warning"})
    options = PdfPipelineOptions(accelerator_options=AcceleratorOptions(device=device), ocr_options=rapid,
                                 do_ocr=ocr, do_formula_enrichment=True)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def side_car(body, placeholders, fallbacks, recognized, filled, scanned, thin_pages):
    notes = []
    if placeholders != fallbacks:
        notes.append(f"[수식] 자리표시자 {placeholders}개, 대체할 수식 {fallbacks}개로 달라 교체하지 않음")
    elif body.count(UNDECODED):
        notes.append(f"[수식] {body.count(UNDECODED)}개 formula-not-decoded 남음 (해당 영역에 PyMuPDF 텍스트 없음)")
    if thin_pages:
        notes.append(f"[쪽] 글자 {SPARSE_CHARS_PER_PAGE}자 미만: p.{', p.'.join(map(str, thin_pages))} (그림 쪽이 아니라면 본문 누락)")
    notes += [f"[{MARK}] …{body[max(0, m.start() - 40):m.end() + 40]}…".replace("\n", " ")
              for m in re.finditer(re.escape(MARK), body)]
    if filled:
        notes.append(f"[확인] 수식 {filled}개는 LaTeX 인식이 어긋나 PyMuPDF 텍스트로 대체함(읽기 순서가 흩어질 수 있음). PDF와 대조")
    if recognized:
        notes.append(f"[확인] 수식 {recognized}개는 모델이 인식한 LaTeX라 첨자·기호 오인식이 있을 수 있음. 인용 전 PDF와 대조")
    if scanned:
        notes.append("[확인] 본문은 OCR 결과라 글자 오류가 있을 수 있음. 인용 전 PDF와 대조")
    return ([f"[판독 요청] 아래 {len(notes)}건은 PDF를 보고 사람이 확인해야 한다"] + notes) if notes else []


def convert(pdf, pages, glyph_map):
    with tempfile.TemporaryDirectory() as workdir:
        pdf_doc, path, first = select_pages(pdf, pages, workdir)
        device = pick_device()
        scanned, why = needs_ocr(pdf_doc)
        print(f"[device] {device}\n[ocr] {'on' if scanned else 'off'} ({why})", file=sys.stderr)
        document = docling_converter(device, ocr=scanned).convert(path).document

    fallbacks, formulas = refine_formulas(document, pdf_doc, glyph_map)
    markdown = document.export_to_markdown()
    placeholders = markdown.count(UNDECODED)
    body, filled = fill_formulas(markdown, fallbacks)
    recognized = formulas - len(fallbacks)
    stats = collections.Counter(formulas=formulas, recognized=recognized, filled=filled, scanned=scanned)
    body = clean(body, stats)
    thin_pages = [] if scanned else [first + n for n in sparse_pages(pdf_doc)]
    dump = "\n".join(f"<<<p{first + n}>>>\n" + clean("\n".join(line_texts(page, glyph_map)), collections.Counter())
                     for n, page in enumerate(pdf_doc))
    return body, stats, side_car(body, placeholders, len(fallbacks), recognized, filled, scanned, thin_pages), dump


def header(cite, pages, stats):
    scope = f"p.{pages} 발췌" if pages else "full text extraction"
    steps = [f"docling 구조 추출{f' (p.{pages})' if pages else ''}",
             "RapidOCR" if stats["scanned"] else "",
             f"수식 {stats['recognized']}/{stats['formulas']}개 LaTeX 인식" if stats["formulas"] else "",
             f"수식 {stats['filled']}개 PyMuPDF 텍스트 대체" if stats["filled"] else "",
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
    assert tidy_latex("x = 1 \\\\ & \\\\ &") == "x = 1" and tidy_latex("a \\frac { b } { c }") == "a \\frac { b } { c }"
    assert tidy_latex("a & = b \\\\ & \\\\ c & = d") == "\\begin{aligned} a & = b \\end{aligned}"
    assert tidy_latex("\\begin{cases} a & b \\\\ & \\\\ c \\end{cases}") == "\\begin{cases} a & b \\\\ & \\\\ c \\end{cases}"
    assert is_degenerate("c " * 30, "J[k]") and is_degenerate("x" * 90, "ab") and not is_degenerate("x = 1", "x = 1 (3)")
    assert EQUATION_NUMBER.search("Q[k] = J[k] · Qp[k], (13)").group(1) == "13" and not EQUATION_NUMBER.search("y(k) = 3")
    import fitz

    blank = fitz.open()
    for _ in range(3):
        blank.new_page()
    assert needs_ocr(blank)[0] is True and sparse_pages(blank) == [0, 1, 2]
    with tempfile.TemporaryDirectory() as workdir:
        path = os.path.join(workdir, "text.pdf")
        text_pdf = fitz.open()
        page = text_pdf.new_page()
        for n, line in enumerate(SAMPLE_LINES):
            page.insert_text((72, 100 + 20 * n), line, fontsize=11)
        text_pdf.save(path)
        for bad in ("3", "3-2", "0-1", "2-9", "a-b"):
            try:
                select_pages(path, bad, workdir)
            except SystemExit:
                continue
            raise AssertionError(bad)
        assert select_pages(path, "1-1", workdir)[0].page_count == 1

        scan_path = os.path.join(workdir, "scan.pdf")
        scan_pdf = fitz.open()
        scan_page = scan_pdf.new_page()
        scan_page.insert_image(scan_page.rect, stream=text_pdf[0].get_pixmap(dpi=150).tobytes("png"))
        scan_pdf.save(scan_path)
        for pdf, expect_scanned, needed in ((path, False, len(SAMPLE_WORDS)), (scan_path, True, 3)):
            body, stats, notes, _ = convert(pdf, None, {})
            found = [w for w in SAMPLE_WORDS if w in body.lower()]
            assert bool(stats["scanned"]) is expect_scanned and len(found) >= needed, (pdf, found, body)
            assert bool(notes) is expect_scanned, notes
    print("selftest ok")


def main():
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        return selftest()
    if argv[:1] == ["--scan"]:
        return scan(argv[1])
    if argv[:1] == ["--crop"]:
        return crop(*argv[1:4])
    silence_known_noise()
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("out")
    parser.add_argument("--cite", required=True)
    parser.add_argument("--pages")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--glyph", action="append", default=[], metavar="FONT:0xHH=CHAR")
    parser.add_argument("--fitz-dump")
    args = parser.parse_args()
    for target in (args.out, args.fitz_dump):
        if target and os.path.exists(target) and not args.force:
            sys.exit(f"{target} 이미 있음 — 덮으려면 --force")
    body, stats, notes, dump = convert(args.pdf, args.pages, parse_glyphs(args.glyph))
    if args.fitz_dump:
        with open(args.fitz_dump, "w", encoding="utf-8") as f:
            f.write(dump)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(header(args.cite, args.pages, stats) + body)
    print(f"wrote {args.out}: {len(body.splitlines())} lines, "
          f"formulas={stats['recognized']}+{stats['filled']}/{stats['formulas']}, marks={stats['mark']}")
    print("\n".join(notes) if notes else "(사이드카 없음)")


if __name__ == "__main__":
    main()
