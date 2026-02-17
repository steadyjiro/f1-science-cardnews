# 🏎️ F1 Science Card News Generator

F1 드라이버 생리학 오픈 액세스 논문을 자동으로 찾아서 카드뉴스 이미지로 만들어주는 시스템입니다.

**예산: 0원 | 자동화: 100% | 주 2회 자동 실행**

---

## 📁 프로젝트 구조

```
f1-science-cardnews/
├── main.py                         ← 메인 파이프라인
├── prompts.py                      ← LLM 프롬프트 3종
├── requirements.txt                ← Python 패키지
├── .github/workflows/
│   └── f1_cardnews.yml             ← 자동 스케줄링
├── templates/                      ← 카드뉴스 HTML 템플릿
│   ├── card_cover.html
│   ├── card_context.html
│   ├── card_finding.html
│   ├── card_implication.html
│   └── card_closing.html
├── data/
│   ├── queries.json                ← 검색 키워드
│   └── processed_papers.json       ← 처리 이력
└── output/                         ← 생성된 카드뉴스 (자동 생성)
    └── 2026-02-17_10-1234_xxxx/
        ├── card_01.png ~ card_07.png
        ├── metadata.json
        └── instagram_caption.txt
```

---

## 🚀 설치 방법 (1회만)

### 1단계: 이 저장소를 GitHub에 올리기

1. github.com에서 새 저장소 생성 (Public)
2. 이 폴더의 모든 파일을 업로드

### 2단계: API 키를 GitHub Secrets에 등록

저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret 이름 | 값 |
|---|---|
| `GEMINI_API_KEY` | aistudio.google.com에서 발급한 키 |
| `GROQ_API_KEY` | console.groq.com에서 발급한 키 |
| `PEXELS_API_KEY` | pexels.com/api에서 발급한 키 |

(Semantic Scholar는 키 없이 작동합니다)

### 3단계: 끝!

매주 월/목 18:00(한국시간)에 자동 실행됩니다.
수동 실행: Actions 탭 → F1 Science Card News Generator → Run workflow

---

## 🔧 작동 원리

1. **논문 검색**: Semantic Scholar에서 F1 생리학 관련 OA 논문 자동 검색
2. **텍스트 추출**: PDF 다운로드 → 텍스트 + 그래프 추출
3. **AI 분석**: Gemini API로 논문 핵심 내용 분석
4. **스크립트 생성**: 7장 카드뉴스 스크립트 자동 작성
5. **팩트체크**: AI가 수치 정확성 + 저작권 자동 검증
6. **이미지 생성**: Pexels 실사 배경 + 논문 그래프 + HTML 템플릿 → PNG
7. **저장**: output/ 폴더에 자동 커밋

---

## 💰 비용

| 항목 | 비용 |
|------|------|
| Semantic Scholar API | 무료 |
| Gemini API (무료 티어) | 무료 |
| GroqCloud (폴백) | 무료 |
| Pexels API | 무료 |
| GitHub Actions | 무료 (공개 저장소) |
| **합계** | **0원** |

---

## ⚠️ 주의사항

- API 키를 코드에 직접 넣지 마세요. 반드시 GitHub Secrets 사용.
- Gemini 무료 티어는 일 250회 제한. 이 시스템은 편당 3~4회만 사용.
- 논문 Figure 재사용은 CC-BY 라이선스일 때만 자동 허용됩니다.
