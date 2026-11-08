"""Fetch candidate papers from arXiv and OpenAlex (journals)."""
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

UA = {"User-Agent": "paper-radar (https://github.com/; personal research alert tool)"}
OPENALEX = "https://api.openalex.org/works"
ARXIV = "https://export.arxiv.org/api/query"
WORK_TYPES = "article|preprint|review|letter"
SELECT = ("id,doi,title,display_name,publication_date,type,authorships,primary_location,"
          "locations,abstract_inverted_index,referenced_works")


def log(*a):
    print("[sources]", *a, flush=True)


# ---------------------------------------------------------------- helpers
def _quote(term):
    term = term.strip()
    return f'"{term}"' if re.search(r"[\s\-]", term) else term


def norm_title(t):
    t = re.sub(r"<[^>]+>", " ", t or "").lower()
    return re.sub(r"[^a-z0-9]+", "", t)[:150]


def _abstract(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _get(url, params, tries=5):
    for k in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=90)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 5 * (k + 1)
                log(f"HTTP {r.status_code} from {url.split('/')[2]}, retry in {wait}s")
                time.sleep(wait)
                continue
            log(f"HTTP {r.status_code}: {r.text[:300]}")
            return None
        except requests.RequestException as e:
            log("network error:", e)
            time.sleep(5 * (k + 1))
    return None


# ---------------------------------------------------------------- OpenAlex
def openalex_bool(q):
    any_part = "(" + " OR ".join(_quote(t) for t in q["any"]) + ")"
    if q.get("and_any"):
        return any_part + " AND (" + " OR ".join(_quote(t) for t in q["and_any"]) + ")"
    return any_part


def _openalex_list(filt, api_key, cap=3000):
    out, cursor = [], "*"
    while cursor and len(out) < cap:
        r = _get(OPENALEX, {"filter": filt, "per-page": 200, "cursor": cursor,
                            "select": SELECT, "api_key": api_key})
        if r is None:
            break
        b = r.json()
        out.extend(b.get("results", []))
        cursor = b.get("meta", {}).get("next_cursor")
        if not b.get("results"):
            break
    return out


def openalex_to_paper(w):
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
    arxiv = None
    for l in [loc] + (w.get("locations") or []):
        u = (l or {}).get("landing_page_url") or ""
        m = re.search(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})", u)
        if m:
            arxiv = m.group(1)
            break
    if not arxiv and doi and doi.startswith("10.48550/arxiv."):
        arxiv = doi.split("arxiv.")[1]
    is_arxiv_src = "arxiv" in (src.get("display_name") or "").lower()
    authors = [a["author"]["display_name"] for a in (w.get("authorships") or []) if a.get("author")]
    author_ids = [a["author"]["id"].rsplit("/", 1)[-1] for a in (w.get("authorships") or [])
                  if a.get("author") and a["author"].get("id")]
    url = f"https://doi.org/{doi}" if doi else (loc.get("landing_page_url") or w["id"])
    if is_arxiv_src and arxiv:
        url = f"https://arxiv.org/abs/{arxiv}"
    return {
        "title": _clean(w.get("display_name") or w.get("title") or ""),
        "authors": authors,
        "author_ids": author_ids,
        "venue": "arXiv" if is_arxiv_src else (src.get("display_name") or ""),
        "publisher": src.get("host_organization_name") or "",
        "date": w.get("publication_date") or "",
        "doi": None if (doi or "").startswith("10.48550/") else doi,
        "arxiv": arxiv,
        "url": url,
        "abstract": _clean(_abstract(w.get("abstract_inverted_index"))),
        "openalex": w["id"].rsplit("/", 1)[-1],
        "refs": [x.rsplit("/", 1)[-1] for x in (w.get("referenced_works") or [])],
        "is_preprint": is_arxiv_src or w.get("type") == "preprint",
        "via": set(),
    }


def openalex_keyword(q, since, api_key):
    filt = (f"title_and_abstract.search:{openalex_bool(q)},"
            f"from_publication_date:{since},type:{WORK_TYPES}")
    works = _openalex_list(filt, api_key)
    log(f"OpenAlex '{q['name']}': {len(works)}")
    return [openalex_to_paper(w) for w in works]


def openalex_cites(key_papers, since, api_key):
    ids = [k["openalex"] for k in key_papers if k.get("openalex")]
    out = []
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        filt = f"cites:{'|'.join(chunk)},from_publication_date:{since},type:{WORK_TYPES}"
        out += [openalex_to_paper(w) for w in _openalex_list(filt, api_key)]
    log(f"OpenAlex citations of key papers: {len(out)}")
    return out


def openalex_authors(authors, since, api_key):
    ids = [a for au in authors for a in au.get("openalex", [])]
    out = []
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        filt = f"authorships.author.id:{'|'.join(chunk)},from_publication_date:{since},type:{WORK_TYPES}"
        out += [openalex_to_paper(w) for w in _openalex_list(filt, api_key)]
    log(f"OpenAlex followed authors: {len(out)}")
    return out


def resolve_key_papers(key_papers, api_key):
    """Fill in missing OpenAlex ids from DOIs (single lookups are free)."""
    for k in key_papers:
        if k.get("openalex") or not k.get("doi"):
            continue
        r = _get(f"{OPENALEX}/doi:{k['doi']}", {"select": "id", "api_key": api_key})
        if r is not None:
            k["openalex"] = r.json()["id"].rsplit("/", 1)[-1]
            log(f"resolved {k['doi']} -> {k['openalex']}")
        else:
            log(f"could not resolve key paper DOI {k['doi']}")


# ---------------------------------------------------------------- arXiv
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv_bool(q):
    def grp(terms):
        parts = []
        for t in terms:
            qt = _quote(t)
            parts += [f"ti:{qt}", f"abs:{qt}"]
        return "(" + " OR ".join(parts) + ")"
    s = grp(q["any"])
    if q.get("and_any"):
        s += " AND " + grp(q["and_any"])
    return s


def arxiv_keyword(q, since, until=None, cap=3000):
    until = until or date.today().isoformat()
    rng = f"submittedDate:[{since.replace('-', '')}0000 TO {until.replace('-', '')}2359]"
    query = f"({arxiv_bool(q)}) AND {rng}"
    out, start, page = [], 0, 200
    while start < cap:
        r = _get(ARXIV, {"search_query": query, "start": start, "max_results": page,
                         "sortBy": "submittedDate", "sortOrder": "descending"})
        if r is None:
            break
        root = ET.fromstring(r.content)
        entries = root.findall("a:entry", NS)
        for e in entries:
            aid_full = e.findtext("a:id", "", NS)
            m = re.search(r"abs/(.+?)(v\d+)?$", aid_full)
            if not m:
                continue
            aid = m.group(1)
            doi = e.findtext("arxiv:doi", None, NS)
            jref = e.findtext("arxiv:journal_ref", None, NS)
            out.append({
                "title": _clean(e.findtext("a:title", "", NS)),
                "authors": [_clean(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)],
                "author_ids": [],
                "venue": "arXiv" + (f" (published: {_clean(jref)})" if jref else ""),
                "publisher": "",
                "date": (e.findtext("a:published", "", NS) or "")[:10],
                "doi": doi.lower() if doi else None,
                "arxiv": aid,
                "url": f"https://arxiv.org/abs/{aid}",
                "abstract": _clean(e.findtext("a:summary", "", NS)),
                "openalex": None,
                "refs": [],
                "is_preprint": True,
                "via": set(),
            })
        time.sleep(3.1)  # arXiv asks for >= 3 s between calls
        if len(entries) < page:
            break
        start += page
    log(f"arXiv '{q['name']}': {len(out)}")
    return out
