#!/usr/bin/env python3
"""
arxiv_scan.py -- daily arXiv digest for the spin-qubit process-tensor project.

Queries the arXiv API for recent submissions in quant-ph and cond-mat.mes-hall,
scores them against a project-specific watchlist of authors and keywords, and
writes a markdown digest (default: digests/YYYY-MM-DD.md).

No dependencies beyond the Python standard library.

Usage:
    python arxiv_scan.py                # last 2 days, write digest file
    python arxiv_scan.py --days 7      # wider window (e.g. after a holiday)
    python arxiv_scan.py --stdout      # print instead of writing a file
"""

import argparse
import datetime as dt
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ----------------------------------------------------------------------------
# Watchlist (edit freely; keep lowercase)
# ----------------------------------------------------------------------------

# Any new paper by these authors is flagged regardless of keywords.
# Matching is on surname + initial to limit false positives.
WATCH_AUTHORS = [
    "strunz",        # Walter T. Strunz (Dresden group)
    "backer",        # Charlotte Baecker (spelled Bäcker/Baecker/Backer)
    "modi",          # Kavan Modi
    "milz",          # Simon Milz
    "pollock",       # Felix A. Pollock
    "giarmatzi",     # Christina Giarmatzi
    "taranto",       # Philip Taranto
    "paz-silva",     # Gerardo Paz-Silva
    "cywinski",      # Lukasz Cywinski
]
# Ambiguous surnames: require BOTH surname and an initial/context keyword hit.
WATCH_AUTHORS_STRICT = {
    "white": ["g. a. l.", "gregory a"],   # G. A. L. White
    "costa": ["f. costa", "fabio"],       # Fabio Costa
    "viola": ["l. viola", "lorenza"],     # Lorenza Viola
    "link": ["v. link", "valentin"],      # Valentin Link
}

# Keyword groups: (score, [phrases]). A paper's score is the sum over groups
# with at least one phrase present in title+abstract (lowercased).
KEYWORD_GROUPS = [
    (5, ["process tensor", "quantum comb", "process matrix"]),
    (5, ["temporal entanglement", "entanglement in time"]),
    (5, ["quantum memory witness", "witness quantum memory",
         "quantumness of memory", "classical memory"]),
    (4, ["multi-time", "multitime"]),
    (3, ["non-markovian", "nonmarkovian", "non markovian"]),
    (3, ["quantum memory"]),
    (3, ["noise spectroscopy"]),
    (3, ["filter function", "filter-function"]),
    (2, ["dynamical decoupling"]),
    (3, ["spin qubit", "spin qubits", "silicon qubit", "donor spin"]),
    (2, ["random telegraph", "two-level fluctuator", "1/f noise"]),
    (2, ["hyperfine", "nuclear spin bath", "central spin"]),
    (2, ["pauli twirl", "correlated noise", "spatiotemporal"]),
    (2, ["optimal control", "grape", "pulse shaping"]),
]

# CRITICAL if score >= this, or if an author hit coincides with any keyword.
CRITICAL_SCORE = 8
RELEVANT_SCORE = 4

CATEGORIES = ["quant-ph", "cond-mat.mes-hall"]
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_RESULTS_PER_CAT = 300   # generous; quant-ph posts ~100-150/day
REQUEST_PAUSE_S = 3         # arXiv API politeness

# ----------------------------------------------------------------------------


def fetch_recent(category: str, max_results: int) -> bytes:
    query = urllib.parse.urlencode({
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": max_results,
    })
    url = f"{ARXIV_API}?{query}"
    last_exc = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent":
                         "arxiv-scan/1.1 (github actions; contact via repo)"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                print(f"  [fetch] {category}: HTTP {resp.status}, "
                      f"{len(data)} bytes (attempt {attempt+1})")
                return data
        except Exception as ex:  # noqa
            last_exc = ex
            print(f"  [fetch] {category}: attempt {attempt+1} failed: "
                  f"{type(ex).__name__}: {ex}")
            time.sleep(5 * (attempt + 1))
    raise last_exc


def parse_feed(xml_bytes: bytes) -> list:
    root = ET.fromstring(xml_bytes)
    entries = []
    for e in root.findall(f"{ATOM}entry"):
        def text(tag):
            node = e.find(f"{ATOM}{tag}")
            return (node.text or "").strip() if node is not None else ""
        authors = [a.find(f"{ATOM}name").text.strip()
                   for a in e.findall(f"{ATOM}author")
                   if a.find(f"{ATOM}name") is not None]
        arxiv_id = text("id").rsplit("/", 1)[-1]
        entries.append({
            "id": arxiv_id,
            "title": re.sub(r"\s+", " ", text("title")),
            "abstract": re.sub(r"\s+", " ", text("summary")),
            "authors": authors,
            "published": text("published"),   # ISO 8601
            "updated": text("updated"),       # ISO 8601 (may differ)
            "link": f"https://arxiv.org/abs/{arxiv_id}",
        })
    return entries


def _parse_iso(s: str):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

def within_days(entry, days: int, now: dt.datetime) -> bool:
    # Accept if published OR updated falls within the window. Robust to the
    # arXiv submittedDate-sort quirks around weekends / UTC boundaries.
    for key in ("published", "updated"):
        t = _parse_iso(entry.get(key, "")) if isinstance(entry, dict) else None
        if t is not None and dt.timedelta(0) <= (now - t) <= dt.timedelta(days=days):
            return True
    return False


def author_hit(entry: dict):
    """Return (strong_hits, weak_hits).

    strong: unambiguous watchlist surnames, or strict surnames whose
            identifying context (initials/first name) is present.
    weak:   strict surnames present but unconfirmed (evidence only)."""
    strong, weak = [], []
    joined = " ; ".join(entry["authors"]).lower()
    for surname in WATCH_AUTHORS:
        if re.search(rf"\b{re.escape(surname)}\b", joined) or \
           (surname == "backer" and re.search(r"b[aä]cker", joined)):
            strong.append(surname)
    for surname, contexts in WATCH_AUTHORS_STRICT.items():
        if re.search(rf"\b{re.escape(surname)}\b", joined):
            if any(c in joined for c in contexts):
                strong.append(surname)
            else:
                weak.append(surname + "?")
    return strong, weak


def keyword_score(entry: dict):
    blob = (entry["title"] + " " + entry["abstract"]).lower()
    score, matched = 0, []
    for pts, phrases in KEYWORD_GROUPS:
        found = [p for p in phrases if p in blob]
        if found:
            score += pts
            matched.extend(found)
    return score, matched


def triage(entry: dict):
    strong, weak = author_hit(entry)
    k_score, k_matched = keyword_score(entry)
    if (strong and k_score > 0) or k_score >= CRITICAL_SCORE:
        label = "CRITICAL"
    elif strong or (weak and k_score > 0) or k_score >= RELEVANT_SCORE:
        label = "RELEVANT"
    elif k_score > 0:
        label = "FYI"
    else:
        label = None
    return label, strong + weak, k_score, k_matched


def format_digest(hits: dict, day: str, days: int, n_scanned: int) -> str:
    lines = [f"# arXiv digest -- {day} (window: last {days} day(s), "
             f"{n_scanned} papers scanned)", ""]
    if hits["CRITICAL"]:
        lines.append("## :rotating_light: CRITICAL -- read today")
    for label in ("CRITICAL", "RELEVANT", "FYI"):
        if label != "CRITICAL":
            lines.append(f"## {label}")
        if not hits[label]:
            lines.append("_none_\n")
            continue
        for e, a_hits, score, matched in hits[label]:
            first_authors = ", ".join(e["authors"][:4])
            if len(e["authors"]) > 4:
                first_authors += " et al."
            lines.append(f"### [{e['title']}]({e['link']})")
            lines.append(f"*{first_authors}* -- `{e['id']}`")
            reasons = []
            if a_hits:
                reasons.append("watched author(s): " + ", ".join(sorted(set(a_hits))))
            if matched:
                reasons.append("keywords: " + ", ".join(sorted(set(matched))[:8]))
            lines.append(f"score {score} -- " + "; ".join(reasons))
            abstract = e["abstract"]
            lines.append("> " + (abstract[:600] + ("..." if len(abstract) > 600 else "")))
            lines.append("")
    if not any(hits.values()):
        lines.append("No hits today.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--outdir", default="digests")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    seen, entries, errors = set(), [], []
    for cat in CATEGORIES:
        try:
            feed = fetch_recent(cat, MAX_RESULTS_PER_CAT)
            parsed = parse_feed(feed)
            kept = 0
            for e in parsed:
                if e["id"] not in seen and within_days(e, args.days, now):
                    seen.add(e["id"])
                    entries.append(e)
                    kept += 1
            print(f"  [parse] {cat}: {len(parsed)} entries in feed, "
                  f"{kept} within last {args.days} day(s)")
            if parsed:
                print(f"  [parse] {cat}: newest published = {parsed[0]['published']}")
        except Exception as ex:                       # network/parse failure
            errors.append(f"{cat}: {type(ex).__name__}: {ex}")
            print(f"  [error] {cat}: {type(ex).__name__}: {ex}")
        time.sleep(REQUEST_PAUSE_S)

    # Holiday safety net: if the window caught nothing but we DID retrieve a
    # feed, report the most recent announcement batch instead of an empty
    # digest, so the scan is never silently vacuous after a long weekend.
    if not entries and not errors:
        pool = []
        for cat in CATEGORIES:
            try:
                pool.extend(parse_feed(fetch_recent(cat, MAX_RESULTS_PER_CAT)))
                time.sleep(REQUEST_PAUSE_S)
            except Exception:
                pass
        # newest published date present in the pool
        dated = [(_parse_iso(e.get("published","")), e) for e in pool]
        dated = [(t, e) for (t, e) in dated if t is not None]
        if dated:
            newest = max(t for t, _ in dated).date()
            seen2 = set()
            for t, e in dated:
                if t.date() == newest and e["id"] not in seen2:
                    seen2.add(e["id"]); entries.append(e)
            print(f"  [fallback] window empty; reporting newest batch "
                  f"{newest} ({len(entries)} papers)")

    if errors and not entries:
        # Total failure: still write a digest so latest.md exists and is honest.
        day = now.date().isoformat()
        os.makedirs(args.outdir, exist_ok=True)
        msg = (f"# arXiv digest -- {day}\n\n"
               f"**Scan error -- no papers retrieved.** The arXiv API could not be "
               f"reached or returned nothing on this run.\n\n"
               + "\n".join(f"- {e}" for e in errors)
               + "\n\nThis is NOT a 'no hits' result; the window was not scanned.\n")
        for name in (f"{day}.md", "latest.md"):
            with open(os.path.join(args.outdir, name), "w") as f:
                f.write(msg)
        print("Scan error; wrote error digest.\n" + "\n".join(errors))
        sys.exit(1)

    hits = {"CRITICAL": [], "RELEVANT": [], "FYI": []}
    for e in entries:
        label, a_hits, score, matched = triage(e)
        if label:
            hits[label].append((e, a_hits, score, matched))
    for label in hits:
        hits[label].sort(key=lambda t: -t[2])

    day = now.date().isoformat()
    digest = format_digest(hits, day, args.days, len(entries))

    if args.stdout:
        print(digest)
        return
    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, f"{day}.md")
    with open(path, "w") as f:
        f.write(digest)
    # Always refresh the stable pointer here in Python, so the workflow
    # never depends on a separate cp step or on a dated filename matching.
    with open(os.path.join(args.outdir, "latest.md"), "w") as f:
        f.write(digest)
    print(f"Wrote {path} and latest.md  ({len(hits['CRITICAL'])} critical, "
          f"{len(hits['RELEVANT'])} relevant, {len(hits['FYI'])} fyi; "
          f"{len(entries)} papers scanned)")


if __name__ == "__main__":
    main()
