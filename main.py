"""
F1 Science Card News — 완전 자동화 파이프라인
GitHub Actions에서 자동 실행됨. 수동 개입 불필요.
"""

import os
import sys
import json
import time
import re
import traceback
from datetime import datetime
from pathlib import Path

import requests
import pdfplumber

# ── 설정 ──
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
SS_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
TEMPLATES_DIR = Path("templates")
HISTORY_FILE = DATA_DIR / "processed_papers.json"
QUERIES_FILE = DATA_DIR / "queries.json"

from prompts import PROMPT_ANALYSIS, PROMPT_CARDNEWS, PROMPT_VERIFY


# =============================================
# STEP 1: 논문 검색 (Semantic Scholar API)
# =============================================
def search_papers():
    """Semantic Scholar에서 F1 생리학 관련 OA 논문 검색"""
    queries = json.loads(QUERIES_FILE.read_text())
    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []

    headers = {}
    if SS_KEY:
        headers["x-api-key"] = SS_KEY

    new_papers = []
    seen_dois = set(history)

    for query in queries:
        try:
            url = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {
                "query": query,
                "limit": 10,
                "fields": "title,authors,year,venue,externalIds,openAccessPdf,abstract,citationCount",
                "openAccessPdf": "",
                "year": "2015-",
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)

            if resp.status_code == 429:
                print(f"[WARN] Rate limited on query: {query}. Waiting 30s...")
                time.sleep(30)
                continue
            if resp.status_code != 200:
                print(f"[WARN] Search failed for '{query}': HTTP {resp.status_code}")
                continue

            data = resp.json()
            for paper in data.get("data", []):
                doi = (paper.get("externalIds") or {}).get("DOI")
                oa_pdf = paper.get("openAccessPdf")
                if doi and oa_pdf and doi not in seen_dois:
                    paper["doi"] = doi
                    paper["pdf_url"] = oa_pdf.get("url", "")
                    new_papers.append(paper)
                    seen_dois.add(doi)

            time.sleep(1.5)  # Rate limit: 1 req/sec for unauthenticated

        except Exception as e:
            print(f"[WARN] Search error for '{query}': {e}")
            continue

    # 인용 수 기준 정렬, 최대 2편 처리
    new_papers.sort(key=lambda p: p.get("citationCount", 0), reverse=True)
    selected = new_papers[:2]

    if selected:
        print(f"✅ Found {len(new_papers)} new papers, selected top {len(selected)}:")
        for p in selected:
            print(f"   - {p.get('title', 'Unknown')} (DOI: {p['doi']})")
    else:
        print("ℹ️ No new papers found.")

    return selected


# =============================================
# STEP 2: PDF 다운로드 + 텍스트 추출
# =============================================
def download_and_extract(paper):
    """PDF 다운로드 → 텍스트 + Figure 추출"""
    pdf_url = paper.get("pdf_url", "")
    abstract = paper.get("abstract", "") or ""
    text = ""
    figures = []
    pdf_path = "/tmp/paper.pdf"

    if pdf_url:
        try:
            print(f"   Downloading PDF: {pdf_url[:80]}...")
            resp = requests.get(pdf_url, timeout=45, headers={
                "User-Agent": "F1ScienceCardNews/1.0 (research automation)"
            })
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(pdf_path, "wb") as f:
                    f.write(resp.content)

                # 텍스트 추출 (pdfplumber)
                with pdfplumber.open(pdf_path) as pdf:
                    pages_text = []
                    for page in pdf.pages[:25]:  # 최대 25페이지
                        pt = page.extract_text()
                        if pt:
                            pages_text.append(pt)
                    text = "\n".join(pages_text)

                # Figure 추출 (PyMuPDF)
                figures = extract_figures_from_pdf(pdf_path)

            else:
                print(f"   [WARN] PDF download failed: HTTP {resp.status_code}")
        except Exception as e:
            print(f"   [WARN] PDF processing error: {e}")

    # 텍스트가 없으면 abstract로 폴백
    if not text.strip():
        text = abstract
        print("   Using abstract only (PDF text extraction failed)")

    # 토큰 절약: 최대 15000자
    if len(text) > 15000:
        text = text[:15000]

    return text, figures, pdf_path


def extract_figures_from_pdf(pdf_path):
    """PDF에서 Figure 후보 이미지 추출 (PyMuPDF)"""
    figures = []
    try:
        import fitz
        doc = fitz.open(pdf_path)
        fig_dir = Path("/tmp/figures")
        fig_dir.mkdir(exist_ok=True)

        for page_num in range(min(len(doc), 25)):
            page = doc[page_num]
            for img_idx, img in enumerate(page.get_images(full=True)):
                try:
                    xref = img[0]
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n >= 5:  # CMYK → RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    # 크기 필터: 300x200 이상만
                    if pix.width >= 300 and pix.height >= 200:
                        fname = f"figure_{page_num}_{img_idx}.png"
                        pix.save(str(fig_dir / fname))
                        figures.append({
                            "filename": fname,
                            "width": pix.width,
                            "height": pix.height,
                            "page": page_num
                        })
                except Exception:
                    continue
        doc.close()
    except ImportError:
        print("   [INFO] PyMuPDF not available, skipping figure extraction")
    except Exception as e:
        print(f"   [WARN] Figure extraction error: {e}")

    print(f"   Extracted {len(figures)} figure candidates")
    return figures


# =============================================
# STEP 3-5: LLM API 호출 (폴백 체인)
# =============================================
def call_llm(prompt):
    """Gemini → Groq → Gemini Flash-Lite 폴백 체인"""
    providers = []

    if GEMINI_KEY:
        providers.append(("gemini", "gemini-2.5-flash-preview-05-20", GEMINI_KEY))
    if GROQ_KEY:
        providers.append(("groq", "llama-3.3-70b-versatile", GROQ_KEY))
    if GEMINI_KEY:
        providers.append(("gemini", "gemini-2.0-flash-lite", GEMINI_KEY))

    if not providers:
        raise Exception("No LLM API keys configured!")

    for provider_type, model, key in providers:
        try:
            if provider_type == "gemini":
                return call_gemini(prompt, key, model)
            elif provider_type == "groq":
                return call_groq(prompt, key, model)
        except Exception as e:
            print(f"   [WARN] {model} failed: {e}")
            time.sleep(5)
            continue

    raise Exception("All LLM providers failed!")


def call_gemini(prompt, api_key, model="gemini-2.5-flash-preview-05-20"):
    """Google Gemini API 호출"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096}
    }
    resp = requests.post(url, json=payload, timeout=60)

    if resp.status_code == 429:
        raise Exception("Gemini rate limited (429)")
    if resp.status_code != 200:
        raise Exception(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return text


def call_groq(prompt, api_key, model="llama-3.3-70b-versatile"):
    """GroqCloud API 호출"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)

    if resp.status_code == 429:
        raise Exception("Groq rate limited (429)")
    if resp.status_code != 200:
        raise Exception(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


def parse_json_response(text):
    """LLM 응답에서 JSON 추출 (코드블록 제거 등)"""
    text = text.strip()
    # Remove markdown code blocks
    if "```" in text:
        matches = re.findall(r'```(?:json)?\s*([\s\S]*?)```', text)
        if matches:
            text = matches[0].strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first { to last }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Failed to parse JSON from LLM response: {text[:200]}...")


# =============================================
# STEP 6: 비주얼 소싱 + 카드 이미지 렌더링
# =============================================
def fetch_pexels_photo(query, output_path):
    """Pexels API에서 사진 다운로드"""
    if not PEXELS_KEY:
        print(f"   [WARN] No Pexels API key, skipping photo for: {query}")
        return None

    try:
        headers = {"Authorization": PEXELS_KEY}
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 5, "orientation": "square"},
            headers=headers, timeout=10
        )
        if resp.status_code == 200:
            photos = resp.json().get("photos", [])
            if photos:
                # 첫 번째 사진의 고해상도 버전
                img_url = photos[0]["src"]["large2x"]
                img_resp = requests.get(img_url, timeout=15)
                if img_resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(img_resp.content)
                    photographer = photos[0].get("photographer", "Unknown")
                    print(f"   📸 Pexels photo saved: {query} (by {photographer})")
                    return output_path
        print(f"   [WARN] Pexels search returned no results for: {query}")
    except Exception as e:
        print(f"   [WARN] Pexels error: {e}")
    return None


def render_cards(cardnews, analysis, figures_dir, output_dir):
    """HTML 템플릿 + Playwright로 카드뉴스 이미지 생성"""
    from jinja2 import Environment, FileSystemLoader
    from playwright.sync_api import sync_playwright

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) Pexels 사진 다운로드
    pexels_cache = {}
    for card in cardnews.get("cards", []):
        query = card.get("pexels_query", "")
        vs = card.get("visual_source", "")
        if vs == "pexels" and query and query not in pexels_cache:
            photo_path = output_dir / f"bg_{card['card_num']}.jpg"
            result = fetch_pexels_photo(query, str(photo_path))
            if result:
                pexels_cache[query] = str(photo_path)

    # 2) Playwright로 렌더링
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1080})

        for card in cardnews.get("cards", []):
            try:
                card_type = card.get("type", "cover")
                template_name = f"card_{card_type}.html"

                # 이미지 경로 결정
                bg_image_path = ""
                figure_caption = card.get("figure_caption", "")
                chart_data = card.get("chart_data", {})

                vs = card.get("visual_source", "")
                if vs == "pexels":
                    query = card.get("pexels_query", "")
                    cached = pexels_cache.get(query)
                    if cached:
                        bg_image_path = f"file://{os.path.abspath(cached)}"
                elif vs == "paper_figure":
                    fig_file = card.get("figure_file", "")
                    if fig_file and figures_dir:
                        fig_path = Path(figures_dir) / fig_file
                        if fig_path.exists():
                            bg_image_path = f"file://{fig_path.absolute()}"

                # 템플릿 렌더링
                template = env.get_template(template_name)
                html = template.render(
                    bg_image_path=bg_image_path,
                    figure_caption=figure_caption,
                    chart_data=chart_data,
                    **{k: v for k, v in card.items()
                       if k not in ("visual_source", "pexels_query", "figure_file", "chart_data", "figure_caption")}
                )

                page.set_content(html, wait_until="networkidle")
                page.wait_for_timeout(800)  # 이미지 로딩 대기

                out_path = output_dir / f"card_{card['card_num']:02d}.png"
                page.screenshot(path=str(out_path))
                print(f"   🎨 Rendered: {out_path.name}")

            except Exception as e:
                print(f"   [WARN] Render error card {card.get('card_num')}: {e}")
                traceback.print_exc()

        browser.close()


# =============================================
# MAIN: 전체 파이프라인 실행
# =============================================
def main():
    print("=" * 60)
    print(f"🏎️ F1 Science Card News Generator — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # STEP 1: 논문 검색
    print("\n📚 STEP 1: Searching papers...")
    papers = search_papers()
    if not papers:
        print("No new papers. Exiting.")
        return

    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []

    for paper in papers:
        doi = paper["doi"]
        title = paper.get("title", "Unknown")
        print(f"\n{'─' * 50}")
        print(f"📄 Processing: {title}")
        print(f"   DOI: {doi}")

        try:
            # STEP 2: 텍스트 + Figure 추출
            print("\n   📥 STEP 2: Downloading & extracting...")
            text, figures, pdf_path = download_and_extract(paper)

            if not text.strip():
                print("   [SKIP] No text extracted.")
                continue

            figure_list_str = json.dumps(figures, indent=2) if figures else "없음 (추출 실패 또는 이미지 없음)"
            figures_dir = "/tmp/figures" if figures else None

            # Determine license (best effort)
            license_str = "Unknown (check paper)"
            oa_info = paper.get("openAccessPdf", {})
            if oa_info:
                license_str = "Open Access (likely CC-BY, verify on publisher site)"

            # STEP 3: 논문 분석
            print("\n   🔬 STEP 3: Analyzing paper...")
            authors_str = ", ".join([
                a.get("name", "Unknown") for a in (paper.get("authors") or [])[:5]
            ])
            analysis_prompt = PROMPT_ANALYSIS.format(
                title=title, authors=authors_str, doi=doi,
                year=paper.get("year", "N/A"), venue=paper.get("venue", "N/A"),
                license=license_str, paper_text=text, figure_list=figure_list_str
            )
            analysis_raw = call_llm(analysis_prompt)
            analysis = parse_json_response(analysis_raw)
            print(f"   ✅ Analysis complete: {analysis.get('hook_headline', '?')}")

            time.sleep(3)

            # STEP 4: 카드뉴스 스크립트
            print("\n   ✍️ STEP 4: Generating card news script...")
            use_figures = analysis.get("figure_selection", {}).get("use_paper_figures", False)
            available_figures = analysis.get("figure_selection", {}).get("selected_figures", [])

            cardnews_prompt = PROMPT_CARDNEWS.format(
                analysis_json=json.dumps(analysis, ensure_ascii=False, indent=2),
                use_figures=str(use_figures),
                available_figures=json.dumps(available_figures)
            )
            cardnews_raw = call_llm(cardnews_prompt)
            cardnews = parse_json_response(cardnews_raw)
            print(f"   ✅ Card script: {len(cardnews.get('cards', []))} cards generated")

            time.sleep(3)

            # STEP 5: 검증
            print("\n   🔍 STEP 5: Verifying accuracy...")
            verify_prompt = PROMPT_VERIFY.format(
                paper_text_excerpt=text[:5000],
                license=license_str,
                analysis_json=json.dumps(analysis, ensure_ascii=False),
                cardnews_json=json.dumps(cardnews, ensure_ascii=False)
            )
            verify_raw = call_llm(verify_prompt)
            verification = parse_json_response(verify_raw)
            verdict = verification.get("verdict", "UNKNOWN")
            print(f"   ✅ Verification: {verdict}")

            # 검증 실패 시 1회 재생성
            if verdict == "REVISION_NEEDED":
                print("   🔄 Revision needed — regenerating card script...")
                revision_instructions = verification.get("revision_instructions", "")
                cardnews_prompt_v2 = PROMPT_CARDNEWS.format(
                    analysis_json=json.dumps(analysis, ensure_ascii=False, indent=2),
                    use_figures=str(use_figures),
                    available_figures=json.dumps(available_figures)
                ) + f"\n\n## 수정 지시 (팩트체커 피드백)\n{revision_instructions}"

                time.sleep(3)
                cardnews_raw_v2 = call_llm(cardnews_prompt_v2)
                cardnews = parse_json_response(cardnews_raw_v2)
                print(f"   ✅ Revised: {len(cardnews.get('cards', []))} cards")

            # STEP 6: 이미지 렌더링
            print("\n   🎨 STEP 6: Rendering card images...")
            date_str = datetime.now().strftime("%Y-%m-%d")
            safe_doi = doi.replace("/", "_").replace(".", "-")[:60]
            run_output_dir = OUTPUT_DIR / f"{date_str}_{safe_doi}"

            render_cards(cardnews, analysis, figures_dir, run_output_dir)

            # 메타데이터 저장
            metadata = {
                "paper": {"title": title, "doi": doi, "year": paper.get("year"),
                          "authors": authors_str, "venue": paper.get("venue")},
                "analysis": analysis,
                "cardnews": cardnews,
                "verification": verification,
                "generated_at": datetime.now().isoformat()
            }
            meta_path = run_output_dir / "metadata.json"
            meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))

            # Instagram caption 저장
            caption = cardnews.get("instagram_caption", "")
            if caption:
                (run_output_dir / "instagram_caption.txt").write_text(caption)

            # STEP 7: 이력 갱신
            history.append(doi)
            HISTORY_FILE.write_text(json.dumps(history, indent=2))

            print(f"\n   🏁 DONE: {run_output_dir}")

        except Exception as e:
            print(f"\n   ❌ ERROR processing {doi}: {e}")
            traceback.print_exc()
            continue

        time.sleep(5)  # API rate limit 방지

    print(f"\n{'=' * 60}")
    print("🏎️ Pipeline complete!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
