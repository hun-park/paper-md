---
name: paper-md
description: 논문 PDF를 markdown으로 변환한다(docling 구조 추출, PyMuPDF 수식 복원, 스캔본은 OCR). "pdf를 md로", "md로 저장", "캐시에 저장" 요청이나 읽어야 할 PDF의 md가 없을 때 쓴다.
---

# paper-md: PDF → md

```bash
uv run --script --locked ${CLAUDE_SKILL_DIR}/paper_md.py <pdf> <out.md> \
    --cite "<저자> <연도>, <학술지> <권>(<호>):<쪽>, DOI <doi>" \
    [--pages 3-9] [--glyph FONT:0xHH=CHAR ...] [--fitz-dump <path>] [--force]
```

- `--cite`는 직접 쓴다. 헤더에 들어간다.
- `--pages a-b`는 발췌. 헤더에 `p.a-b 발췌`로 적힌다.
- 기존 md가 있으면 멈춘다. `--force`로 덮어쓴다.
- `--fitz-dump`는 `--force` 없이는 덮어쓰지 않는다. PyMuPDF 페이지 본문(`<<<pN>>>` 구분)을 쓴다. 수식 확인 참조용이다.
- 자가검증: `paper_md.py --selftest`. 순수 함수 검사에 더해 합성 PDF(텍스트, 스캔 이미지)를 실제로 변환한다. 처음에는 모델을 내려받는다.

## 동작

| 단계 | 내용 |
|---|---|
| 장치·OCR 판정 | CUDA가 있으면 GPU, 없으면 CPU. 글자가 100자 미만인 쪽이 절반 이상이면 스캔본으로 보고 RapidOCR과 docling 수식 인식(LaTeX `$$…$$`)을 켠다. stderr에 `[device]`, `[ocr] on/off (사유)` |
| docling 본문 추출 | 섹션·리스트·캡션이 분리된 markdown. 수식은 `<!-- formula-not-decoded -->`로 남는다 |
| PyMuPDF 수식 복원 | 자리표시자마다 docling이 준 쪽 위치(bbox)의 PyMuPDF 텍스트(글리프 매핑 적용)로 교체. 자리표시자 수와 수식 수가 다르면 교체하지 않는다 |
| 정리 | 하이픈 병합, 리가처, 특수 공백·soft hyphen 정규화 |
| `⟦?⟧` 표시 | 복원되지 않은 C0 제어문자, U+FFFD, 비공개 영역 글리프, `(cid:N)` 토큰 |

## 사이드카 (stdout)

`[판독 요청]` 줄이 나오면 아래 항목은 사람이 PDF를 보고 판단할 일이다. 추측으로 채우지 말고 해당 항목과 위치를 사용자에게 보여 주며 판독을 요청한다.

- `[수식] 자리표시자 N개, 수식 항목 M개로 달라 교체하지 않음` — 개수가 안 맞아 문서 전체의 수식 교체를 포기한 것.
- `[수식] N개 formula-not-decoded 남음` — 해당 위치에 PyMuPDF 텍스트가 없어 교체하지 못한 수식.
- `[쪽] 글자 100자 미만: p.N` — OCR이 꺼진 상태에서 글자가 거의 없는 쪽. 그림 쪽이 아니라면 본문 누락.
- `[⟦?⟧] …` — 소실 글리프와 앞뒤 40자. 문맥으로 하나로 확정될 때만 복원하고, 아니면 `⟦?⟧`로 두고 요청한다.
- `[확인] …` — 복원·인식한 수식과 OCR 결과. 인용하기 전에 PDF와 대조를 요청한다.

사이드카가 비어 있으면 요청할 항목이 없다.

## 글리프 판정 (`--scan`, `--crop`, `--glyph`)

수식 기호가 엉뚱한 글자로 나올 때:

1. `paper_md.py --scan <pdf>` — 의심 (폰트, 코드)를 건수·예시와 함께 센다.
2. `paper_md.py --crop <pdf> FONT:0xHH[:n] <out.png>` — 그 자리를 잘라 본다.
3. `--glyph FONT:0xHH=CHAR`로 검증한 매핑을 전달한다.
4. 문서·폰트 단위로만 매핑한다. 같은 코드도 폰트마다 다르다.
