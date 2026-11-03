# job-pay-ranges

Pull the **published pay ranges** from Greenhouse, Lever and Ashby job boards into one CSV, and see how many postings on a board actually state pay, where the number came from, and what period it is for.

All three applicant tracking systems have a public, read-only job board API, and all three can return a structured pay range when the company fills it in. Many companies don't, and write the range into the description instead. This tool reads the structured field first, falls back to ranges written in the description, and tells you which one it used for every job.

- Python 3.9 or newer, standard library only. No API keys.
- One request per board. A `User-Agent` that names the tool, a 1-second pause between boards, and backoff on HTTP 429 and 5xx.
- MIT licensed.

## Quick start

```bash
python3 job_pay_ranges.py greenhouse:cloudflare lever:zoox ashby:ramp --csv pay.csv
```

Each argument is `ats:board`, where the board name is the last part of the company's public job board URL: `job-boards.greenhouse.io/cloudflare` (or `boards.greenhouse.io/cloudflare`) becomes `greenhouse:cloudflare`, `jobs.lever.co/zoox` becomes `lever:zoox` (use `lever-eu:` for boards on `jobs.eu.lever.co`), and `jobs.ashbyhq.com/ramp` becomes `ashby:ramp`.

It prints a coverage summary per board as JSON and, with `--csv`, writes one row per job. `--save-raw DIR` also keeps each board's raw JSON response.

## Where each ATS keeps pay

| ATS | Endpoint | Structured pay | Pay period |
|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/BOARD/jobs?content=true&pay_transparency=true` | `pay_input_ranges[]`: `min_cents`, `max_cents`, `currency_type`, `title`, `blurb` | No field. Read from the range `title` (for example "Canada Annual Pay Range") or its `blurb`, otherwise unknown. |
| Lever | `api.lever.co/v0/postings/COMPANY?mode=json` | `salaryRange`: `min`, `max`, `currency`, `interval` | `interval`. Lever doesn't document the values; seen so far: `per-year-salary`, `per-hour-wage`. Other values are passed through as-is in `pay_text`, and `interval` is filled only if the code recognises the value. |
| Ashby | `api.ashbyhq.com/posting-api/job-board/BOARD?includeCompensation=true` | `compensation.compensationTiers[].components[]` with `compensationType`, `minValue`, `maxValue`, `currencyCode` | `interval`: `1 YEAR`, `1 MONTH`, and so on |

Things the code handles because the APIs do them:

- **Greenhouse amounts are in cents**, and Greenhouse's `content` is HTML that has been escaped once more, so it is unescaped twice before searching it.
- **Greenhouse needs `pay_transparency=true`**; without it, `pay_input_ranges` isn't in the response at all.
- **Ashby mixes base pay with equity, commission and bonus** in the same list of components. Only `compensationType: "Salary"` is used.
- **Ashby has `shouldDisplayCompensationOnJobPostings`.** When it is `false`, the structured range is not used.
- **Ashby has `isListed`.** Jobs with `isListed: false` are skipped, because Ashby's docs say they should only be available via direct link.
- **More than one range per job** (several location tiers, or several cities written in the description): the row keeps the first one, `range_count` says how many there were, and `pay_text` lists them all.

## The CSV

| Column | Meaning |
|---|---|
| `ats`, `board`, `job_id`, `title`, `location`, `url` | The job. |
| `pay_source` | `ats` (structured field), `description` (a range written in the text) or `none`. |
| `pay_min`, `pay_max`, `currency`, `interval` | The first range, as published. `interval` is `year`, `month`, `week`, `day`, `hour`, or empty when it can't be told. |
| `annual_min`, `annual_max` | The range per year in the same currency: hour × 2,080, day × 260, week × 52, month × 12. Empty when the period is unknown. No currency conversion. |
| `range_count`, `pay_text` | How many ranges the job has, and their text. |

## Ranges written in the description

The fallback looks for a currency symbol or code followed by a range: `$120,000 - $150,000`, `€60.000 – €75.000`, `$120-150K`, `SEK 760,000 - SEK 1,240,000`, `140,000 - 190,000 USD`. It looks at the words around the range for the period (`per hour`, `/hr`, `annual`, `per month`...). A range of 10,000 or more next to the word "salary" counts as yearly. Anything else stays unknown rather than guessed.

It misses ranges with typos inside the number (`€63, 000`), currency names written out between the number and the code (`418K-574K Swedish SEK`), and ranges with no currency at all. Treat `pay_source = description` as "probably right, check a sample", and `none` as "no range this tool could read", not "no pay information".

## Tests

```bash
python3 -m unittest discover -s tests -v
```

25 tests, all offline, against small made-up board files in `tests/fixtures/` (the company is "example"). They cover cents conversion and the pay period on Greenhouse, hourly annualizing on Lever, ignoring equity and commission on Ashby, the hidden-compensation flag, skipping unlisted Ashby jobs, the description fallback (symbols, codes before and after, `K` suffixes, European thousands separators, reversed ranges), the coverage summary, and the retry logic (429 with `Retry-After` is retried; 404 is not).

Last run (Nov 2, 2026, ET): 25 passed, 0 failed on Python 3.9, 3.10, 3.11, 3.12, 3.13 and 3.14 on Linux; pyflakes reports nothing.

## Use it politely

These are public endpoints that companies use to power their own careers pages. Read a board, don't hammer it: one request per board is enough, and the job lists rarely change more than a few times a day. Pay ranges are published for candidates; if you store or republish them, check the board's terms and the law where you are.

## Sources

- Greenhouse Job Board API (`pay_transparency`, `pay_input_ranges`): https://developers.greenhouse.io/job-board.html
- Lever Postings API (`salaryRange`): https://github.com/lever/postings-api
- Ashby public job posting API (`includeCompensation`): https://developers.ashbyhq.com/docs/public-job-posting-api

## License and disclaimers

MIT. See [LICENSE](LICENSE). This code and README were written with AI assistance. Not affiliated with or endorsed by Greenhouse, Lever or Ashby.
