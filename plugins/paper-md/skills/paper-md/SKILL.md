---
name: paper-md
description: refs/md/<stem>.md를 원본 PDF에서 생성한다 — ① docling으로 구조화된 markdown 추출 ② pymupdf로 수식 복원. refs/에서 논문을 읽어야 하는데 md/<stem>.md가 없을 때, 기존 md를 다시 만들 때, "캐시에 저장", "md로 저장", "pdf를 md로" 요청에 쓴다. 발췌 메모·요약을 md/에 손으로 쓰지 말 것 — md/에는 이 스킬의 산출물만 둔다.
---

# paper-md: REFS 캐시 생성 (PDF → md)

`refs/`는 read-through 캐시다. `md/<stem>.md`가 있으면 그것을 읽고, 없으면 원본 PDF(`cited/` = 본문이 `\cite`하는 것, stem은 bib 키 / `related/` = 인용하지 않는 것)를 이 스킬로 변환해 `md/<stem>.md`에 저장한다.

**md에 들어가는 것은 두 단계의 산출물뿐이다.** ① docling 구조화 본문 ② pymupdf 수식 복원. 발췌·요약·해설은 md가 아니다 — 필요하면 CLAUDE.md나 커밋 메시지에 둔다.

## 1. 스크립트

```bash
S=${CLAUDE_SKILL_DIR}
uv run --script --locked $S/paper_md.py <pdf> <out.md> \
    --cite "<저자> <연도>, <학술지> <권>(<호>):<쪽>, DOI <doi>" \
    [--pages 3-9] [--glyph FONT:0xHH=CHAR ...] [--fitz-dump <scratchpad>/<stem>.fitz.txt] [--force]
```

- `--cite`는 직접 쓴다. refs.bib에서 읽지 않는다(`related/`는 bib 키가 아니다).
- `--pages a-b`는 발췌. 헤더에 `p.a-b 발췌`로 적힌다.
- 기존 md가 있으면 멈춘다. `--force`로 덮어쓴다.
- `--fitz-dump`는 pymupdf 페이지 본문(`<<<pN>>>` 구분)을 쓴다. 수식 확인 참조용이며 `md/` 밖에 둔다.
- 자가검증: `uv run --script --locked $S/paper_md.py --selftest`.

스크립트가 하는 일:

| 단계 | 내용 |
|---|---|
| docling 본문 추출 | PDF → 구조화된 markdown (섹션·리스트·캡션 분리). 수식은 `<!-- formula-not-decoded -->`로 표시 |
| PyMuPDF 수식 복원 | `<!-- formula-not-decoded -->` 자리표시자를 PyMuPDF에서 추출한 번호 붙은 수식 텍스트로 교체 |
| 하이픈 병합·리가처·특수 공백 | `com-\nplex`→`complex`, `ﬁ`→`fi`, 비분리 하이픈·soft hyphen·얇은 공백 정규화 |
| 소실 글리프 표시 `⟦?⟧` | C0 제어문자, U+FFFD, 기호 폰트 PUA 중 복원되지 않은 것 |

헤더는 자동으로 만들어진다:

```
<cite> -- full text extraction (개인 연구 참조용).
조판 정리: docling 구조 추출, 수식 N/M개 PyMuPDF 복원, ...
======================================================================
```

## 2. 사이드카 (stdout)

- `[수식] N개 미복원 formula-not-decoded 남음` — PyMuPDF 번호 수식과 매칭되지 않은 것. PDF를 보고 수동 삽입.
- `[⟦?⟧] …` — 소실 글리프와 앞뒤 40자. 문맥으로 확정되면 복원. 확정되지 않으면 `⟦?⟧`로 둔다.

## 3. 글리프 판정 (`--scan`, `--crop`, `--glyph`)

수식 기호가 엉뚱한 글자로 나올 때:

1. `paper_md.py --scan <pdf>` — 의심 (폰트, 코드)를 건수·예시와 함께 센다.
2. `paper_md.py --crop <pdf> FONT:0xHH[:n] <out.png>` — 그 자리를 잘라 본다.
3. `--glyph FONT:0xHH=CHAR`로 검증한 매핑을 전달한다.
4. **문서·폰트 단위로만** 매핑한다. 같은 코드도 폰트마다 다르다.

## 4. 원본을 받을 수 없을 때

PDF 없이는 md를 만들지 않는다. 출판사가 다운로드를 막으면 사용자에게 수동 다운로드를 부탁한다.
