"""Paper Radar: find new papers on your topic and email a daily digest.

Run:  python -m radar.main            (normal daily run)
      python -m radar.main --days 30  (look back 30 days this time)
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import sources, emailer
from .ai import Scorer

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"
PAPERS = ROOT / "docs" / "data" / "papers.json"
STATUS = ROOT / "docs" / "data" / "status.json"
IST = timezone(timedelta(hours=5, minutes=30))


def log(*a):
    print("[radar]", *a, flush=True)


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def aliases(p):
    a = set()
    if p.get("doi"):
        a.add("doi:" + p["doi"].lower())
    if p.get("arxiv"):
        a.add("arxiv:" + p["arxiv"])
    if p.get("openalex"):
        a.add("oa:" + p["openalex"])
    t = sources.norm_title(p.get("title"))
    if len(t) > 25:
        a.add("title:" + t)
    return a


def merge(into, new):
    """Combine two records of the same paper; prefer the journal version for display."""
    journal_first = into.get("is_preprint") and not new.get("is_preprint")
    if journal_first:
        for f in ("title", "venue", "publisher", "date", "url", "is_preprint"):
            into[f] = new.get(f) or into.get(f)
    for f in ("doi", "arxiv", "openalex"):
        into[f] = into.get(f) or new.get(f)
    if len(new.get("abstract") or "") > len(into.get("abstract") or ""):
        into["abstract"] = new["abstract"]
    if len(new.get("authors") or []) > len(into.get("authors") or []):
        into["authors"] = new["authors"]
    into["author_ids"] = list(set(into.get("author_ids", [])) | set(new.get("author_ids", [])))
    into["refs"] = list(set(into.get("refs", [])) | set(new.get("refs", [])))
    into["via"] |= new["via"]


def collect(cfg, since, oa_key):
    pool, index = [], {}
    stats = {"arxiv": 0, "openalex": 0, "cites": 0, "authors": 0}

    def add(papers, via, stat):
        stats[stat] += len(papers)
        for p in papers:
            if not p.get("title"):
                continue
            p["via"] = {via}
            al = aliases(p)
            hit = next((index[a] for a in al if a in index), None)
            if hit is None:
                pool.append(p)
                hit = len(pool) - 1
            else:
                merge(pool[hit], p)
            for a in al | aliases(pool[hit]):
                index[a] = hit

    for q in cfg["queries"]:
        add(sources.arxiv_keyword(q, since), "keyword", "arxiv")
        add(sources.openalex_keyword(q, since, oa_key), "keyword", "openalex")
    sources.resolve_key_papers(cfg.get("key_papers", []), oa_key)
    add(sources.openalex_cites(cfg.get("key_papers", []), since, oa_key), "cites", "cites")
    add(sources.openalex_authors(cfg.get("authors", []), since, oa_key), "author", "authors")
    return pool, stats


def _norm_name(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", re.sub(r"[.\-]", " ", n)).split()


def annotate(p, cfg):
    kp = {k["openalex"]: k["short"] for k in cfg.get("key_papers", []) if k.get("openalex")}
    p["cites"] = sorted({kp[r] for r in p.get("refs", []) if r in kp})
    ids = set(p.get("author_ids", []))
    names = {" ".join(_norm_name(a)) for a in p.get("authors", [])}
    followed = []
    for au in cfg.get("authors", []):
        toks = _norm_name(au["name"])
        variants = {" ".join(toks), f"{toks[0]} {toks[-1]}"}
        if ids & set(au.get("openalex", [])) or names & variants:
            followed.append(au["name"])
    p["followed"] = followed
    pv = cfg.get("preferred_venues", {})
    venue, pub = (p.get("venue") or "").lower(), (p.get("publisher") or "").lower()
    p["preferred"] = (any(venue.startswith(j.lower()) for j in pv.get("journals", []))
                      or any(x.lower() in pub for x in pv.get("publishers", [])))


def public_record(p, group, today):
    return {
        "key": p["key"], "title": p["title"], "authors": p.get("authors", [])[:10],
        "n_authors": len(p.get("authors", [])), "venue": p.get("venue", ""),
        "date": p.get("date", ""), "url": p.get("url", ""), "doi": p.get("doi"),
        "arxiv": p.get("arxiv"), "abstract": (p.get("abstract") or "")[:900],
        "score": p.get("score"), "category": p.get("category", ""), "reason": p.get("reason", ""),
        "preferred": p.get("preferred", False), "cites": p.get("cites", []),
        "followed": p.get("followed", []), "is_preprint": p.get("is_preprint", False),
        "group": group, "found": today,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="look back this many days (overrides default)")
    ap.add_argument("--dry-run", action="store_true", help="do not send email or save anything")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--max-ai", type=int, help="score at most N candidates (testing)")
    ap.add_argument("--preview", help="write the email HTML to this file")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    nv_key, oa_key = os.environ.get("NVIDIA_API_KEY", ""), os.environ.get("OPENALEX_API_KEY", "")
    mail_id, mail_token = os.environ.get("MAIL_ID", ""), os.environ.get("MAIL_TOKEN", "").replace(" ", "")
    mail_to = os.environ.get("MAIL_TO") or mail_id
    pages_url = os.environ.get("PAGES_URL", "")

    state = load_json(STATE, {"seen": {}, "runs": 0})
    first_run = state.get("runs", 0) == 0
    run_cfg = cfg.get("run", {})
    days = args.days or (run_cfg.get("first_run_days", 90) if first_run else run_cfg.get("window_days", 45))
    today_ist = datetime.now(IST).date()
    today = today_ist.isoformat()
    since = (today_ist - timedelta(days=days)).isoformat()
    log(f"{'FIRST RUN' if first_run else 'run'}: looking back {days} days (since {since})")

    pool, stats = collect(cfg, since, oa_key)
    seen = state.get("seen", {})
    fresh = []
    for p in pool:
        al = aliases(p)
        if al & seen.keys():
            continue
        p["aliases"] = sorted(al)
        p["key"] = sorted(al)[0]
        annotate(p, cfg)
        fresh.append(p)
    stats["candidates"] = len(fresh)
    stats["days"] = days
    log(f"{len(pool)} unique papers found, {len(fresh)} not seen before")

    # ---- AI relevance scoring
    to_score = fresh[: args.max_ai] if args.max_ai else fresh
    scores, scorer = {}, None
    if nv_key and to_score:
        scorer = Scorer(nv_key, cfg["ai"], cfg["topic"])
        scores = scorer.score(to_score)
    stats["model"] = scorer.used_model if scorer else None
    unscored = [p for p in to_score if p["key"] not in scores]
    if unscored:
        stats["ai_note"] = (f"The AI filter could not score {len(unscored)} candidates today; "
                            f"they will be retried in the next run.")

    hi, keep = cfg["ai"].get("high_score", 7), cfg["ai"].get("keep_score", 4)
    high, maybe, by_auth, processed = [], [], [], []
    for p in to_score:
        s = scores.get(p["key"])
        if s:
            p.update(s)
        if p["followed"]:
            by_auth.append(p)
            processed.append(p)       # author papers are always shown, even unscored
        elif s:
            processed.append(p)
            if s["score"] >= hi:
                high.append(p)
            elif s["score"] >= keep:
                maybe.append(p)

    def rank(p):
        return ((p.get("score") or 0) + 0.5 * p["preferred"] + 0.5 * bool(p["cites"]), p.get("date", ""))
    high.sort(key=rank, reverse=True)
    maybe.sort(key=rank, reverse=True)
    by_auth.sort(key=lambda p: p.get("date", ""), reverse=True)
    log(f"highly relevant: {len(high)}, possibly: {len(maybe)}, followed authors: {len(by_auth)}")

    # ---- email
    day_label = today_ist.strftime("%d %b %Y")
    subject, html_body, text_body = emailer.build(high, maybe, by_auth, stats, run_cfg, pages_url,
                                                  first_run, day_label)
    if args.preview:
        Path(args.preview).write_text(html_body, encoding="utf-8")
        log("email preview written to", args.preview)
    email_ok = None
    if not (args.dry_run or args.no_email):
        if mail_id and mail_token:
            try:
                emailer.send(mail_id, mail_token, mail_to, subject, html_body, text_body)
                email_ok = True
                log("email sent:", subject)
            except Exception as e:
                email_ok = False
                log("EMAIL FAILED:", e)
        else:
            log("MAIL_ID / MAIL_TOKEN not set; skipping email")

    if args.dry_run:
        log("dry run: nothing saved")
        return

    # ---- save state and web page data
    for p in processed:
        for a in p["aliases"]:
            seen[a] = today
    cutoff = (today_ist - timedelta(days=150)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["runs"] = state.get("runs", 0) + 1
    state["last_run"] = datetime.now(IST).isoformat(timespec="minutes")
    save_json(STATE, state)

    papers = load_json(PAPERS, [])
    papers = ([public_record(p, "high", today) for p in high] +
              [public_record(p, "maybe", today) for p in maybe] +
              [public_record(p, "author", today) for p in by_auth] + papers)
    year_ago = (today_ist - timedelta(days=365)).isoformat()
    papers = [p for p in papers if p["found"] >= year_ago]
    save_json(PAPERS, papers)

    save_json(STATUS, {
        "last_run": state["last_run"], "first_run": first_run, "days": days,
        "candidates": stats["candidates"], "new_high": len(high), "new_maybe": len(maybe),
        "new_authors": len(by_auth), "model": stats["model"], "email_sent": email_ok,
        "sources": {k: stats[k] for k in ("arxiv", "openalex", "cites", "authors")},
        "topic": cfg["topic"]["description"].strip(),
        "authors": [a["name"] for a in cfg.get("authors", [])],
        "key_papers": [k["short"] for k in cfg.get("key_papers", [])],
    })
    log("saved state and web page data")
    if email_ok is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
