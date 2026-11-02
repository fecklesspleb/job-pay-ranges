#!/usr/bin/env python3
"""Pull published pay ranges from Greenhouse, Lever and Ashby job boards.

Each ATS has a public, read-only job board API, and each one can return a
structured pay range when the company fills it in:

* Greenhouse: ``pay_input_ranges`` (needs ``pay_transparency=true``)
* Lever: ``salaryRange``
* Ashby: ``compensation`` (needs ``includeCompensation=true``)

When there is no structured range, the job description is searched for a
written range such as "$120,000 - $150,000". Every row says which one it used.

Standard library only. MIT licensed. https://github.com/fecklesspleb/job-pay-ranges
"""

import argparse
import csv
import html
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0.0"

USER_AGENT = "job-pay-ranges/%s (+https://github.com/fecklesspleb/job-pay-ranges)" % __version__

# Same multipliers as most payroll maths: 40 hours x 52 weeks, 5 days x 52 weeks.
ANNUAL_MULTIPLIER = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}

FIELDS = [
    "ats", "board", "job_id", "title", "location", "url",
    "pay_source", "pay_min", "pay_max", "currency", "interval",
    "annual_min", "annual_max", "range_count", "pay_text",
]


# --------------------------------------------------------------------------
# HTTP


def board_url(ats, board):
    """Return the public API URL for one board."""
    slug = urllib.parse.quote(board, safe="")
    if ats == "greenhouse":
        return "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true&pay_transparency=true" % slug
    if ats == "lever":
        return "https://api.lever.co/v0/postings/%s?mode=json" % slug
    if ats == "lever-eu":
        return "https://api.eu.lever.co/v0/postings/%s?mode=json" % slug
    if ats == "ashby":
        return "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % slug
    raise ValueError("unknown ATS %r (use greenhouse, lever, lever-eu or ashby)" % ats)


def fetch_json(url, retries=3, timeout=60, opener=urllib.request.urlopen, sleep=time.sleep):
    """GET a URL and parse JSON. Retries 429 and 5xx with backoff."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            retryable = err.code == 429 or 500 <= err.code < 600
            if not retryable or attempt == retries:
                raise
            wait = 2 ** attempt * 2
            retry_after = err.headers.get("Retry-After") if err.headers else None
            if retry_after and retry_after.isdigit():
                wait = max(wait, int(retry_after))
            sleep(wait)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# Text helpers


def html_to_text(value):
    """Strip tags. Greenhouse escapes its HTML once more, so unescape twice."""
    if not value:
        return ""
    text = html.unescape(html.unescape(value))
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def annualize(value, interval):
    if value is None or interval not in ANNUAL_MULTIPLIER:
        return None
    return round(value * ANNUAL_MULTIPLIER[interval])


def interval_from_words(text):
    """Map words like 'Annual', 'per hour', '/hr' to year/month/week/day/hour."""
    t = (text or "").lower()
    checks = [
        ("hour", r"\bhourly\b|per hour|an hour|/\s?h(?:ou)?r\b|\bhr\b"),
        ("day", r"\bdaily\b|per day|a day\b|/\s?day\b"),
        ("week", r"\bweekly\b|per week|a week\b|/\s?w(?:ee)?k\b"),
        ("month", r"\bmonthly\b|per month|a month\b|/\s?mo(?:nth)?\b"),
        ("year", r"\bannual(?:ly|ized)?\b|\byearly\b|per year|per annum|a year\b|/\s?y(?:ea)?r\b"),
    ]
    for name, pattern in checks:
        if re.search(pattern, t):
            return name
    return None


# --------------------------------------------------------------------------
# Ranges written in the description

_CURRENCY_PREFIX = r"(?P<cur>US\$|CA\$|C\$|A\$|AU\$|\$|£|€|USD|CAD|GBP|EUR|AUD|SEK|CHF)\s?"
_NUMBER = r"\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?"
_RANGE = re.compile(
    _CURRENCY_PREFIX + r"\s?(?P<lo>" + _NUMBER + r")\s?(?P<lok>[kK])?"
    + r"\s*(?:-|–|—|to)\s*"
    + r"(?:US\$|CA\$|C\$|A\$|AU\$|\$|£|€|USD|CAD|GBP|EUR|AUD|SEK|CHF)?\s?(?P<hi>" + _NUMBER + r")\s?(?P<hik>[kK])?"
    + r"(?:\s?(?P<code>USD|CAD|GBP|EUR|AUD|SEK|CHF)\b)?"
)
# "120,000 - 150,000 USD": no symbol in front, so the code after it is required.
_RANGE_CODE_AFTER = re.compile(
    r"(?<![\w$£€.,])(?P<lo>" + _NUMBER + r")\s?(?P<lok>[kK])?\s*(?:-|–|—|to)\s*"
    + r"(?P<hi>" + _NUMBER + r")\s?(?P<hik>[kK])?\s?(?P<code>USD|CAD|GBP|EUR|AUD|SEK|CHF)\b"
)
_SYMBOL_CURRENCY = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR", "CA$": "CAD", "C$": "CAD", "A$": "AUD", "AU$": "AUD"}


def _parse_number(raw, k_suffix):
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):        # 120.000 (European thousands)
        raw = raw.replace(".", "")
    else:
        raw = raw.replace(",", "")
    value = float(raw)
    if k_suffix:
        value *= 1000
    return value


def ranges_from_text(text):
    """Find written pay ranges. Returns a list of dicts (may be empty)."""
    text = text or ""
    found = []
    matches = list(_RANGE.finditer(text))
    taken = [(m.start(), m.end()) for m in matches]
    matches += [m for m in _RANGE_CODE_AFTER.finditer(text)
                if not any(a < m.end() and m.start() < b for a, b in taken)]
    matches.sort(key=lambda m: m.start())
    for m in matches:
        lo_k = m.group("lok") or m.group("hik")       # "$120-150K" means both are thousands
        lo = _parse_number(m.group("lo"), lo_k)
        hi = _parse_number(m.group("hi"), m.group("hik"))
        if hi < lo:
            continue
        prefix = m.groupdict().get("cur")
        cur = m.group("code") or _SYMBOL_CURRENCY.get(prefix, prefix)
        # Look for the pay period near the range: a little before it, a little after it.
        window = text[max(0, m.start() - 80):m.end() + 40]
        interval = interval_from_words(window)
        if interval is None and lo >= 10000 and re.search(r"\bsalary\b", window, re.I):
            interval = "year"                        # "salary range is $120,000 - $150,000"
        found.append({
            "min": lo, "max": hi, "currency": cur, "interval": interval,
            "text": m.group(0).strip(),
        })
    return found


# --------------------------------------------------------------------------
# Per-ATS parsing


def _row(ats, board, job_id, title, location, url):
    row = {f: None for f in FIELDS}
    row.update({"ats": ats, "board": board, "job_id": job_id, "title": title,
                "location": location, "url": url, "pay_source": "none", "range_count": 0})
    return row


def _fill(row, source, ranges):
    first = ranges[0]
    row.update({
        "pay_source": source,
        "pay_min": first["min"], "pay_max": first["max"],
        "currency": first["currency"], "interval": first["interval"],
        "annual_min": annualize(first["min"], first["interval"]),
        "annual_max": annualize(first["max"], first["interval"]),
        "range_count": len(ranges),
        "pay_text": " | ".join(r["text"] for r in ranges if r.get("text")) or None,
    })
    return row


def _fallback(row, description_html=None, description_text=None):
    text = description_text if description_text else html_to_text(description_html)
    ranges = ranges_from_text(text)
    if ranges:
        _fill(row, "description", ranges)
    return row


def parse_greenhouse(board, data):
    rows = []
    for job in data.get("jobs", []):
        row = _row("greenhouse", board, str(job.get("id")), job.get("title"),
                   (job.get("location") or {}).get("name"), job.get("absolute_url"))
        ranges = []
        for r in job.get("pay_input_ranges") or []:
            if r.get("min_cents") is None or r.get("max_cents") is None:
                continue
            # Greenhouse has no period field. The range title ("Canada Annual Pay Range")
            # or the blurb next to it usually says it; otherwise it stays unknown.
            interval = interval_from_words(r.get("title")) or interval_from_words(html_to_text(r.get("blurb")))
            ranges.append({"min": r["min_cents"] / 100, "max": r["max_cents"] / 100,
                           "currency": r.get("currency_type"), "interval": interval,
                           "text": r.get("title")})
        if ranges:
            _fill(row, "ats", ranges)
        else:
            _fallback(row, description_html=job.get("content"))
        rows.append(row)
    return rows


LEVER_INTERVALS = {
    "per-year-salary": "year", "per-month-salary": "month", "per-week-salary": "week",
    "per-day-wage": "day", "per-hour-wage": "hour",
}


def parse_lever(board, data, ats="lever"):
    rows = []
    for job in data or []:
        row = _row(ats, board, job.get("id"), job.get("text"),
                   (job.get("categories") or {}).get("location"), job.get("hostedUrl"))
        sr = job.get("salaryRange") or {}
        if sr.get("min") is not None and sr.get("max") is not None:
            interval = LEVER_INTERVALS.get(sr.get("interval"))
            _fill(row, "ats", [{"min": float(sr["min"]), "max": float(sr["max"]),
                                "currency": sr.get("currency"), "interval": interval,
                                "text": sr.get("interval")}])
        else:
            text = " ".join(filter(None, [job.get("salaryDescriptionPlain"), job.get("descriptionPlain"),
                                          job.get("additionalPlain")]))
            _fallback(row, description_text=text)
        rows.append(row)
    return rows


ASHBY_INTERVALS = {"1 YEAR": "year", "1 MONTH": "month", "1 WEEK": "week", "1 DAY": "day", "1 HOUR": "hour"}


def parse_ashby(board, data):
    rows = []
    for job in data.get("jobs", []):
        # Ashby's docs: isListed false means "only available via direct link". Skip those.
        if job.get("isListed") is False:
            continue
        row = _row("ashby", board, job.get("id"), job.get("title"), job.get("location"), job.get("jobUrl"))
        comp = job.get("compensation") or {}
        ranges = []
        # Only base pay. Ashby also lists equity, commission and bonus components.
        for tier in comp.get("compensationTiers") or []:
            for c in tier.get("components") or []:
                if c.get("compensationType") != "Salary" or c.get("minValue") is None or c.get("maxValue") is None:
                    continue
                ranges.append({"min": float(c["minValue"]), "max": float(c["maxValue"]),
                               "currency": c.get("currencyCode"),
                               "interval": ASHBY_INTERVALS.get(c.get("interval")),
                               "text": c.get("summary")})
        if ranges and job.get("shouldDisplayCompensationOnJobPostings") is not False:
            _fill(row, "ats", ranges)
        else:
            _fallback(row, description_html=job.get("descriptionHtml"), description_text=job.get("descriptionPlain"))
        rows.append(row)
    return rows


def parse_board(ats, board, data):
    if ats == "greenhouse":
        return parse_greenhouse(board, data)
    if ats in ("lever", "lever-eu"):
        return parse_lever(board, data, ats=ats)
    if ats == "ashby":
        return parse_ashby(board, data)
    raise ValueError("unknown ATS %r" % ats)


# --------------------------------------------------------------------------
# Summary


def summarize(rows):
    """Coverage per board: how many jobs have a structured range, a written one, or none."""
    out = {}
    for row in rows:
        key = "%s:%s" % (row["ats"], row["board"])
        s = out.setdefault(key, {"jobs": 0, "ats": 0, "description": 0, "none": 0,
                                 "interval_unknown": 0, "multiple_ranges": 0,
                                 "currencies": {}, "intervals": {}, "_mid": {}})
        s["jobs"] += 1
        s[row["pay_source"]] += 1
        if row["pay_source"] == "none":
            continue
        if row["range_count"] > 1:
            s["multiple_ranges"] += 1
        s["currencies"][row["currency"]] = s["currencies"].get(row["currency"], 0) + 1
        s["intervals"][row["interval"] or "unknown"] = s["intervals"].get(row["interval"] or "unknown", 0) + 1
        if row["interval"] is None:
            s["interval_unknown"] += 1
        if row["annual_min"] is not None and row["annual_max"] is not None:
            s["_mid"].setdefault(row["currency"], []).append((row["annual_min"] + row["annual_max"]) / 2)
    for s in out.values():
        mids = s.pop("_mid")
        s["median_annual_midpoint"] = {cur: round(statistics.median(v)) for cur, v in sorted(mids.items(), key=lambda kv: str(kv[0]))}
    return out


# --------------------------------------------------------------------------
# CLI


def parse_target(value):
    if ":" not in value:
        raise argparse.ArgumentTypeError("use ats:board, for example greenhouse:example")
    ats, board = value.split(":", 1)
    ats = ats.strip().lower()
    if ats not in ("greenhouse", "lever", "lever-eu", "ashby") or not board.strip():
        raise argparse.ArgumentTypeError("use greenhouse:, lever:, lever-eu: or ashby: followed by the board name")
    return ats, board.strip()


def main(argv=None):
    p = argparse.ArgumentParser(description="Published pay ranges from Greenhouse, Lever and Ashby job boards.")
    p.add_argument("targets", nargs="+", type=parse_target, metavar="ats:board",
                   help="for example greenhouse:example lever:example ashby:example")
    p.add_argument("--csv", help="write one row per job to this CSV file")
    p.add_argument("--save-raw", metavar="DIR", help="also save each board's raw JSON response in DIR")
    p.add_argument("--delay", type=float, default=1.0, help="seconds to wait between boards (default 1)")
    args = p.parse_args(argv)

    all_rows = []
    for i, (ats, board) in enumerate(args.targets):
        if i:
            time.sleep(args.delay)
        url = board_url(ats, board)
        try:
            data = fetch_json(url)
        except (urllib.error.URLError, ValueError) as err:
            print("%s:%s failed: %s" % (ats, board, err), file=sys.stderr)
            continue
        if args.save_raw:
            import os
            os.makedirs(args.save_raw, exist_ok=True)
            with open(os.path.join(args.save_raw, "%s-%s.json" % (ats, re.sub(r"[^\w.-]", "_", board))), "w", encoding="utf-8") as f:
                json.dump(data, f)
        all_rows.extend(parse_board(ats, board, data))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(all_rows)
    print(json.dumps(summarize(all_rows), indent=2, ensure_ascii=False))
    return 0 if all_rows else 1


if __name__ == "__main__":
    sys.exit(main())
