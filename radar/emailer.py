"""Build and send the daily digest email through Gmail."""
import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

INK, MUTED, LINE, ACCENT, SOFT = "#1d2433", "#5f6b7a", "#e3e7ee", "#0b6e69", "#eef6f5"


def _authors(p, n=4):
    a = p.get("authors") or []
    return ", ".join(a[:n]) + (" et al." if len(a) > n else "")


def _badges(p):
    b = []
    if p.get("is_preprint"):
        b.append("arXiv preprint" if (p.get("venue") or "").startswith("arXiv") else "Preprint")
    else:
        b.append("Journal")
    for a in p.get("followed", []):
        b.append("&#128100; " + html.escape(a))
    if p.get("preferred"):
        b.append("&#9733; preferred venue")
    for c in p.get("cites", [])[:2]:
        b.append("cites " + html.escape(c))
    if p.get("category") and p["category"] != "Other":
        b.append(html.escape(p["category"]))
    return " &middot; ".join(b)


def _card(p, full=True):
    e = html.escape
    title = f'<a href="{e(p["url"])}" style="color:{INK};text-decoration:none;font-weight:600">{e(p["title"])}</a>'
    meta = f'{e(_authors(p))} &middot; <i>{e(p.get("venue") or "")}</i> &middot; {e(p.get("date") or "")}'
    score = p.get("score")
    sc = (f'<span style="display:inline-block;min-width:22px;padding:1px 6px;border-radius:9px;'
          f'background:{SOFT};color:{ACCENT};font-weight:700;font-size:12px">{score}</span> ') if score is not None else ""
    out = (f'<div style="padding:12px 0;border-top:1px solid {LINE}">'
           f'<div style="font-size:15px;line-height:1.35">{sc}{title}</div>'
           f'<div style="font-size:12.5px;color:{MUTED};margin-top:3px">{meta}</div>')
    if full and p.get("reason"):
        out += f'<div style="font-size:13.5px;color:{INK};margin-top:5px">{e(p["reason"])}</div>'
    out += f'<div style="font-size:11.5px;color:{ACCENT};margin-top:4px">{_badges(p)}</div></div>'
    return out


def _section(title, papers, full, limit, total_note=""):
    if not papers:
        return ""
    shown = papers[:limit]
    more = len(papers) - len(shown)
    s = (f'<h2 style="font-size:16px;color:{INK};margin:26px 0 4px">{html.escape(title)} '
         f'<span style="color:{MUTED};font-weight:400">({len(papers)})</span></h2>')
    s += "".join(_card(p, full) for p in shown)
    if more > 0:
        s += f'<p style="font-size:13px;color:{MUTED}">+ {more} more on the web page{total_note}.</p>'
    return s


def build(high, maybe, by_authors, stats, cfg_run, pages_url, first_run, day_label):
    n_new = len(high) + len(maybe) + len(by_authors)
    if first_run:
        subject = f"Paper Radar: first run, last {stats['days']} days ({len(high)} highly relevant)"
    elif n_new == 0:
        subject = f"Paper Radar {day_label}: no new papers today"
    else:
        parts = [f"{len(high)} highly relevant"] if high else []
        if maybe:
            parts.append(f"{len(maybe)} possibly relevant")
        if by_authors:
            parts.append(f"{len(by_authors)} from followed authors")
        subject = f"Paper Radar {day_label}: " + ", ".join(parts)

    head = (f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
            f'max-width:680px;margin:0 auto;color:{INK}">'
            f'<div style="border-bottom:3px solid {ACCENT};padding-bottom:8px">'
            f'<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{ACCENT}">Paper Radar</div>'
            f'<div style="font-size:21px;font-weight:700;margin-top:2px">New papers for {html.escape(day_label)}</div></div>')
    body = head
    body += (f'<p style="font-size:12px;color:{MUTED};margin:8px 0 0">The number before each title is the AI '
             f'relevance score (0-10) for your topic.</p>')
    if first_run:
        body += (f'<p style="font-size:14px">This is the first run, so it covers the last '
                 f'{stats["days"]} days. From tomorrow you will only get papers that are new since the previous email.</p>')
    if n_new == 0:
        body += (f'<p style="font-size:14px;margin-top:18px">No new relevant papers today. '
                 f'The radar checked {stats["candidates"]} new candidates from arXiv and journals.</p>')
    body += _section("Highly relevant", high, True, cfg_run.get("email_max_high", 60))
    body += _section("Possibly relevant", maybe, False, cfg_run.get("email_max_maybe", 60))
    body += _section("From authors you follow", by_authors, True, cfg_run.get("email_max_authors", 40))
    if stats.get("ai_note"):
        body += f'<p style="font-size:13px;color:#a24a00;margin-top:18px">{html.escape(stats["ai_note"])}</p>'
    body += (f'<div style="margin-top:28px;padding-top:10px;border-top:1px solid {LINE};font-size:12px;color:{MUTED}">'
             f'Checked {stats["candidates"]} new candidates (arXiv: {stats["arxiv"]}, journals/OpenAlex: {stats["openalex"]}, '
             f'citing key papers: {stats["cites"]}, followed authors: {stats["authors"]}). '
             f'AI model: {html.escape(stats.get("model") or "none")}.<br>'
             f'<a href="{html.escape(pages_url)}" style="color:{ACCENT}">Open the web page</a> to see every paper found so far.'
             f'</div></div>')

    text = [subject, ""]
    for title, ps in (("HIGHLY RELEVANT", high), ("POSSIBLY RELEVANT", maybe), ("FROM AUTHORS YOU FOLLOW", by_authors)):
        if ps:
            text.append(title)
            for p in ps[:60]:
                text.append(f"- {p['title']} ({p.get('venue')}, {p.get('date')})\n  {p['url']}")
                if p.get("reason"):
                    text.append(f"  {p['reason']}")
            text.append("")
    text.append(f"Web page: {pages_url}")
    return subject, body, "\n".join(text)


def send(mail_id, mail_token, mail_to, subject, html_body, text_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Paper Radar <{mail_id}>"
    msg["To"] = mail_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(mail_id, mail_token)
        s.sendmail(mail_id, [x.strip() for x in mail_to.split(",")], msg.as_string())
