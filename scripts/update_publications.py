#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ORCID -> lab-site publication sync.

ORCID로 신규 논문을 찾고, 서지정보는 Crossref에서 받아 index.html의
g(Publications) / w(Home feed) 배열과 About Us 문구를 갱신한다.

설계 원칙
  1. ORCID는 "무엇이 새로 나왔는지" 찾는 용도. 저자/제목/권호는 Crossref가 출처.
     (Google Scholar든 ORCID든 저자 목록이 잘려 나오는 사고가 실제로 있었음)
  2. 사람 검토 전에는 절대 main에 반영하지 않는다. 이 스크립트는 파일만 고치고,
     워크플로가 Pull Request를 만든다.
  3. highlight / summary 는 TODO 자리표시자로 넣는다. PR에서 직접 써야 머지 가능.

사용:
    python scripts/update_publications.py                # index.html 수정
    python scripts/update_publications.py --dry-run      # 파일 안 고치고 보고만
"""

import argparse, json, re, sys, time, urllib.request, urllib.error
from datetime import date

ORCID_ID   = "0000-0002-3569-0983"
INDEX_PATH = "index.html"
PR_BODY    = "pr_body.md"
UA         = {"User-Agent": "lab-site-publication-sync/1.0 (mailto:noreply@krict.re.kr)"}

# 사이트에 올리지 않는 것
EXCLUDE_DOI_PREFIXES = (
    "10.2139",    # SSRN 프리프린트 — 게재본과 중복됨
    "10.14579",   # 막학회지 (Membrane Journal / Membr. J) — 랩 정책상 제외
)
EXCLUDE_TYPES = {"preprint", "other", "dissertation-thesis"}

SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


# ----------------------------------------------------------------- utilities
def get_json(url, tries=3):
    for i in range(tries):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers={**UA, "Accept": "application/json"}),
                timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))
    return None


SUB_MAP = "₀₁₂₃₄₅₆₇₈₉"


def clean_title(t):
    """Crossref/ORCID 제목의 <sub>/<sup>/HTML 태그와 날것의 개행을 정리한다."""
    t = t or ""
    t = re.sub(r"<sub>\s*(\d)\s*</sub>", lambda m: SUB_MAP[int(m.group(1))], t, flags=re.I)
    t = re.sub(r"<sup>\s*(.*?)\s*</sup>", r"\1", t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([\u2080-\u2089])", r"\1", t)   # "CO ₂" -> "CO₂"
    return t.strip()


def normalize_title(t):
    t = (t or "").translate(SUBSCRIPTS)
    for ch in ("\u2010", "\u2011", "\u2013", "\u2014"):
        t = t.replace(ch, "-")
    t = re.sub(r"<[^>]+>", "", t)          # ORCID/Crossref 제목에 <i> 등이 섞여 옴
    return re.sub(r"[^a-z0-9]", "", t.lower())


def extract_array(html, anchor):
    """진짜 괄호 매칭으로 배열을 잘라낸다. 게으른 정규식은 배열 밖까지 먹는다."""
    m = re.search(anchor, html)
    if not m:
        raise SystemExit("anchor를 찾을 수 없음: %s — 번들이 재빌드된 것 같다." % anchor)
    start = m.start(1)
    depth = 0; i = start; instr = False; q = ""
    while i < len(html):
        c = html[i]
        if instr:
            if c == "\\":
                i += 2; continue
            if c == q:
                instr = False
        else:
            if c in "\"'":
                instr = True; q = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return html[start:i + 1], start
        i += 1
    raise SystemExit("배열 괄호가 안 맞음: %s" % anchor)


def js(s):
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    # Crossref 제목에는 날것의 개행/탭이 섞여 온다. JS 문자열 리터럴이 깨지므로 제거.
    return re.sub(r"[\x00-\x1f\u2028\u2029]+", " ", s).strip()


def to_subscript(s):
    """CO2 -> CO₂ 등, 사이트 표기 통일."""
    return re.sub(r"\b(CO|CH|N|H|O|C)(\d)\b",
                  lambda m: m.group(1) + "₀₁₂₃₄₅₆₇₈₉"[int(m.group(2))], s or "")


def format_authors(cr_authors):
    out = []
    for a in cr_authors:
        last = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if not last:
            nm = (a.get("name") or "").strip()
            if nm:
                out.append(nm)
            continue
        ini = []
        for fp in given.split():
            fp = fp.strip(".")
            if not fp:
                continue
            if "-" in fp:
                ini.append("-".join(p[0] + "." for p in fp.split("-") if p))
            else:
                ini.append(fp[0] + ".")
        out.append(("%s, %s" % (last, " ".join(ini))).strip().rstrip(","))
    return "; ".join(out)


def make_volume(vol, page, artno):
    vol = (vol or "").strip()
    pg = (artno or page or "").strip()
    if vol and pg:
        return "Vol. %s, %s" % (vol, pg)
    return pg or (("Vol. " + vol) if vol else "")


TAG_RULES = [
    (r"zif-?8", "ZIF-8"),
    (r"\bzif\b|uio-?66|\bmil-|hkust|metal[- ]organic|mof", "MOF"),
    (r"\bcof\b|covalent organic", "COF"),
    (r"\bhof\b|hydrogen[- ]bonded|epigallocatechin", "HOF"),
    (r"ionic liquid", "Ionic Liquid"),
    (r"pebax|pdms|polyimide|copolymer|polymer|\bpva\b|chitosan|pvdf|elastomeric", "Polymer"),
    (r"mixed[- ]matrix|\bmmm\b|thin[- ]film composite|\btfc\b", "TFC-MMM"),
    (r"facilitated transport", "Facilitated Transport"),
    (r"co2|co₂", "CO₂ Separation"),
    (r"(^|[^a-z])h2[/ ]|/h2", "H₂ Separation"),
    (r"co/n2|co[- ]selective", "CO Separation"),
    (r"\breview\b", "Review"),
]


def infer_tags(title, journal):
    blob = ((title or "") + " " + (journal or "")).translate(SUBSCRIPTS).lower()
    tags = []
    for pat, tag in TAG_RULES:
        if re.search(pat, blob) and tag not in tags:
            tags.append(tag)
    return tags or ["Membrane"]


# ----------------------------------------------------------------- main flow
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index", default=INDEX_PATH)
    args = ap.parse_args()

    html = open(args.index, encoding="utf-8").read()
    g_raw, _ = extract_array(html, r"let g=(\[\{year:\d{4})")
    w_raw, _ = extract_array(html, r"let w=(\[)")

    n_year = len(re.findall(r"year:\d{4}", g_raw))
    n_title = len(re.findall(r'title:"', g_raw))
    if n_year != n_title:
        raise SystemExit("g 배열 추출이 잘못됨 (year %d / title %d)" % (n_year, n_title))
    g_before, w_before = n_year, len(re.findall(r'title:"', w_raw))

    site_dois = {d.lower() for d in re.findall(r'doi:"([^"]+)"', g_raw)}
    site_titles = {normalize_title(t) for t in re.findall(r'title:"((?:[^"\\]|\\.)*)"', g_raw)}

    # --- ORCID: 신규 후보 DOI 수집 -------------------------------------
    works = get_json("https://pub.orcid.org/v3.0/%s/works" % ORCID_ID)
    if not works:
        raise SystemExit("ORCID 조회 실패")

    candidates, skipped = [], []
    for grp in works.get("group", []):
        s = grp["work-summary"][0]
        title = s["title"]["title"]["value"]
        wtype = (s.get("type") or "").lower()
        doi = None
        for e in (grp.get("external-ids") or {}).get("external-id", []):
            if e.get("external-id-type") == "doi":
                doi = e["external-id-value"].lower().strip()
        if not doi:
            skipped.append(("DOI 없음", title)); continue
        if doi.startswith(EXCLUDE_DOI_PREFIXES):
            skipped.append(("프리프린트/막학회지 제외", title)); continue
        if wtype in EXCLUDE_TYPES:
            skipped.append(("type=%s 제외" % wtype, title)); continue
        if doi in site_dois or normalize_title(title) in site_titles:
            continue
        candidates.append((doi, title))

    report = []
    g_entries, w_entries = [], []

    for doi, orcid_title in candidates:
        cr = get_json("https://api.crossref.org/works/" + doi)
        if not cr:
            report.append("⚠️ **Crossref 조회 실패 — 추가하지 않음**: %s (`%s`)" % (orcid_title, doi))
            continue
        msg = cr["message"]
        title = clean_title((msg.get("title") or [orcid_title])[0])
        if normalize_title(title) != normalize_title(orcid_title):
            report.append("⚠️ **제목 불일치 — 추가하지 않음**: ORCID `%s` vs Crossref `%s`"
                          % (orcid_title[:60], title[:60]))
            continue
        if normalize_title(title) in site_titles:
            continue

        authors_raw = msg.get("author") or []
        authors = format_authors(authors_raw)
        names = [((a.get("given", "") + " " + a.get("family", "")).strip()) for a in authors_raw]
        pos = next((i + 1 for i, n in enumerate(names) if "Miso Kang" in n), None)

        journal = clean_title((msg.get("container-title") or [""])[0])
        volume = make_volume(msg.get("volume"), msg.get("page"), msg.get("article-number"))
        dp = (msg.get("published-print") or msg.get("published-online")
              or msg.get("issued") or {}).get("date-parts", [[None]])[0]
        year = dp[0] or 0
        month = dp[1] if len(dp) > 1 else 7
        ttype = "review" if "review" in title.lower() else "journal"
        title_s = to_subscript(title)
        tags = infer_tags(title, journal)

        g = ('{year:%d,type:"%s",title:"%s",authors:"%s",journal:"%s"'
             % (year, ttype, js(title_s), js(authors), js(journal)))
        if volume:
            g += ',volume:"%s"' % js(volume)
        g += ',doi:"%s",tags:[%s],highlight:"TODO: 한 문장 요약을 여기에 작성하세요 (25단어 이내)"}' \
             % (js(doi), ",".join('"%s"' % js(t) for t in tags))
        g_entries.append((year, month, g))

        w = ('{date:"%s",sortKey:%d,type:"paper",title:"%s",'
             'summary:"TODO: 2~3문장 요약을 여기에 작성하세요",journal:"%s",doi:"%s",authors:"%s"}'
             % (date(year, month, 1).strftime("%B %Y"), year * 100 + month,
                js(title_s), js(journal), js(doi), js(authors)))
        w_entries.append((year, month, w))

        flag = ""
        if pos is None:
            flag = " 🚨 **저자 목록에서 Kang, M.을 찾지 못함 — 반드시 확인**"
        report.append(
            "### %s\n"
            "- 저널: %s, %s (%d)\n- DOI: [`%s`](https://doi.org/%s)\n"
            "- 저자 %d명 · **Kang, M. 위치: %s번째** · 마지막 저자: **%s**%s\n"
            "- 자동 태그: %s\n- ⚠️ `highlight` / `summary` 가 TODO 상태입니다. 머지 전에 작성하세요."
            % (title, journal, volume or "-", year, doi, doi,
               len(names), pos if pos else "?", names[-1] if names else "?", flag,
               " / ".join(tags)))

    if not g_entries:
        print("신규 논문 없음.")
        lines = ["ORCID 기준 신규 논문이 없습니다."]
        if skipped:
            lines.append("")
            lines.append("<details><summary>제외된 항목 %d건</summary>\n" % len(skipped))
            lines += ["- %s: %s" % (r, t) for r, t in skipped]
            lines.append("</details>")
        open(PR_BODY, "w", encoding="utf-8").write("\n".join(lines))
        return 0

    g_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    w_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if args.dry_run:
        print("\n\n".join(report))
        print("\n[dry-run] 신규 %d편 — 파일은 수정하지 않음" % len(g_entries))
        return 0

    # --- 패치 ----------------------------------------------------------
    for pat in (r"let g=\[\{year:\d{4}", r"let w=\["):
        if len(re.findall(pat, html)) != 1:
            raise SystemExit("앵커 %r 가 유일하지 않음 — 패치 중단" % pat)

    pos_g = re.search(r"(let g=)(\[)", html).start(2)
    html = html[:pos_g] + "[" + ",".join(e[2] for e in g_entries) + "," + html[pos_g + 1:]
    pos_w = re.search(r"(let w=)(\[)", html).start(2)
    html = html[:pos_w] + "[" + ",".join(e[2] for e in w_entries) + "," + html[pos_w + 1:]

    g_after, _ = extract_array(html, r"let g=(\[\{year:\d{4})")
    w_after, _ = extract_array(html, r"let w=(\[)")
    total = len(re.findall(r"year:\d{4}", g_after))

    html, n_sub = re.subn(r"over \d+ peer-reviewed publications\.",
                          "over %d peer-reviewed publications." % total, html)
    if n_sub != 1:
        report.append("⚠️ About Us 문구 치환이 %d회 — 수동 확인 필요" % n_sub)

    # --- 검증 (실패하면 아무것도 쓰지 않는다) ----------------------------
    assert total == g_before + len(g_entries), "g 개수 불일치"
    assert total == len(re.findall(r'title:"', g_after)), "g year/title 불일치"
    assert len(re.findall(r'title:"', w_after)) == w_before + len(w_entries), "w 개수 불일치"
    assert html.count("{") == html.count("}"), "중괄호 불균형"
    assert html.count("[") == html.count("]"), "대괄호 불균형"
    for _, _, e in g_entries + w_entries:
        assert not re.search(r"[\x00-\x1f]", e), "엔트리에 제어문자 포함 — JS가 깨진다"

    open(args.index, "w", encoding="utf-8").write(html)

    body = ["## ORCID에서 신규 논문 %d편을 찾았습니다" % len(g_entries), "",
            "Publications %d → %d편. About Us 숫자도 함께 갱신했습니다." % (g_before, total), "",
            "> **머지 전 확인:** 아래 저자 위치가 실제와 맞는지, "
            "그리고 `highlight` / `summary` TODO를 채웠는지 확인하세요.", ""]
    body += report
    if skipped:
        body += ["", "<details><summary>제외된 항목 %d건</summary>\n" % len(skipped)]
        body += ["- %s: %s" % (r, t) for r, t in skipped]
        body += ["</details>"]
    open(PR_BODY, "w", encoding="utf-8").write("\n".join(body))

    print("신규 %d편 반영. Publications %d -> %d" % (len(g_entries), g_before, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
