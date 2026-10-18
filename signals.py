#!/usr/bin/env python3
"""Hiring Signal Account Alerts.

Finds companies that just posted a job for the role you sell to (for example
"GTM Engineer"), drops job boards and staffing agencies, and adds the people
who own the stack there (RevOps, GTM Ops, Growth) so you know who to talk to.

Data layer: Crustdata POST /job/search and POST /person/search
(https://docs.crustdata.com).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

DEFAULT_ROLES = "GTM Engineer,GTM Systems Engineer,GTM AI Engineer,Go-to-Market Engineer"
DEFAULT_OWNERS = "Revenue Operations,RevOps,GTM Operations,Sales Operations,Head of Growth"
DEFAULT_HEADCOUNT = "11-50,51-200,201-500"

# Companies that repost other companies' jobs.
RESELLER_INDUSTRIES = {"staffing and recruiting", "human resources services", "job boards"}
RESELLER_NAMES = re.compile(r"\b(jobgether|ladders|recruit|recruiting|staffing|talent partners|headhunt|jobs? board|dice|hirexe)\b", re.I)

# People whose title mentions RevOps but who sell it (agencies) or are not owners.
NOT_AN_OWNER = re.compile(r"\b(account executive|sales development|business development|recruiter|intern)\b", re.I)

JOB_FIELDS = [
    "crustdata_job_id",
    "job_details.title",
    "job_details.url",
    "company.basic_info.crustdata_company_id",
    "company.basic_info.name",
    "company.basic_info.primary_domain",
    "company.basic_info.industries",
    "company.headcount.total",
    "company.headcount.range",
    "location.raw",
    "metadata.date_added",
]
PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "social_handles.professional_network_identifier",
    "experience.employment_details.current",
]


def job_filters(roles: List[str], since: str, country: Optional[str], headcount: List[str]) -> Dict[str, Any]:
    conds: List[Dict[str, Any]] = [
        {"field": "job_details.title", "type": "in", "value": roles},
        {"field": "metadata.date_added", "type": "=>", "value": since},
    ]
    if country:
        conds.append({"field": "location.country", "type": "=", "value": country})
    if headcount:
        conds.append({"field": "company.headcount.range", "type": "in", "value": headcount})
    return {"op": "and", "conditions": conds}


def is_reseller(company: Dict[str, Any]) -> bool:
    info = company.get("basic_info") or {}
    industries = {i.lower() for i in info.get("industries") or []}
    return bool(industries & RESELLER_INDUSTRIES) or bool(RESELLER_NAMES.search(info.get("name") or ""))


def group_accounts(jobs: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """Group postings by company. Returns {"accounts": [...], "dropped": [names]}."""
    by_company: Dict[Any, Dict[str, Any]] = {}
    dropped = set()
    for job in jobs:
        company = job.get("company") or {}
        info = company.get("basic_info") or {}
        if is_reseller(company):
            dropped.add(info.get("name") or "?")
            continue
        key = info.get("crustdata_company_id") or (info.get("name") or "").lower()
        if not key:
            continue
        acct = by_company.setdefault(key, {
            "company_id": info.get("crustdata_company_id"),
            "company": info.get("name", ""),
            "domain": normalize_domain(info.get("primary_domain")),
            "headcount": (company.get("headcount") or {}).get("total"),
            "headcount_range": (company.get("headcount") or {}).get("range", ""),
            "posts": 0, "titles": set(), "latest_post": "", "job_url": "", "locations": set(), "owners": [],
        })
        acct["posts"] += 1
        acct["titles"].add((job.get("job_details") or {}).get("title", ""))
        acct["locations"].add((job.get("location") or {}).get("raw", ""))
        posted = day((job.get("metadata") or {}).get("date_added"))
        if posted >= acct["latest_post"]:
            acct["latest_post"] = posted
            acct["job_url"] = (job.get("job_details") or {}).get("url", "")
        # fill gaps from later rows of the same company
        acct["domain"] = acct["domain"] or normalize_domain(info.get("primary_domain"))
        acct["headcount"] = acct["headcount"] or (company.get("headcount") or {}).get("total")
        acct["headcount_range"] = acct["headcount_range"] or (company.get("headcount") or {}).get("range", "")
    accounts = sorted(by_company.values(), key=lambda a: (a["latest_post"], a["posts"]), reverse=True)
    for a in accounts:
        a["titles"] = sorted(t for t in a["titles"] if t)
        a["locations"] = sorted(l for l in a["locations"] if l)
    return {"accounts": accounts, "dropped": sorted(dropped)}


def owner_filters(company_ids: List[int], owner_titles: List[str]) -> Dict[str, Any]:
    return {"op": "and", "conditions": [
        {"field": "experience.employment_details.current.company_id", "type": "in", "value": company_ids},
        {"op": "or", "conditions": [
            {"field": "experience.employment_details.current.title", "type": "(.)", "value": t} for t in owner_titles]},
    ]}


def attach_owners(client: Crustdata, accounts: List[Dict[str, Any]], owner_titles: List[str],
                  per_company: int = 3, batch: int = 25) -> None:
    by_id = {a["company_id"]: a for a in accounts if a.get("company_id")}
    ids = list(by_id)
    lowered = [t.lower() for t in owner_titles]
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        for profile in client.person_search(owner_filters(chunk, owner_titles), PERSON_FIELDS,
                                            max_results=per_company * len(chunk) * 2):
            for r in ((profile.get("experience") or {}).get("employment_details") or {}).get("current") or []:
                acct = by_id.get(r.get("crustdata_company_id"))
                title = (r.get("title") or "")
                if acct is None or NOT_AN_OWNER.search(title) or not any(t in title.lower() for t in lowered):
                    continue
                if len(acct["owners"]) >= per_company or any(o["person_id"] == profile.get("crustdata_person_id") for o in acct["owners"]):
                    continue
                acct["owners"].append({
                    "person_id": profile.get("crustdata_person_id"),
                    "name": (profile.get("basic_profile") or {}).get("name", ""),
                    "title": title,
                    "profile_url": (((profile.get("social_handles") or {}).get("professional_network_identifier")) or {}).get("profile_url", ""),
                })


def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    accts = result["accounts"]
    owner_titles = Counter()
    for a in accts:
        for o in a["owners"]:
            owner_titles[o["title"]] += 1
    return {
        "job_posts": result["posts"],
        "accounts": len(accts),
        "dropped_resellers": len(result["dropped"]),
        "accounts_with_stack_owner": sum(1 for a in accts if a["owners"]),
        "by_headcount": dict(Counter(a["headcount_range"] or "unknown" for a in accts).most_common()),
    }


def slack_text(result: Dict[str, Any], roles: List[str], since: str, limit: int = 12) -> str:
    accts = result["accounts"]
    lines = [f":mag: {len(accts)} companies posted {' / '.join(roles[:2])} roles since {since}"]
    for a in accts[:limit]:
        owners = ", ".join(f"{o['name']} ({o['title']})" for o in a["owners"]) or "no stack owner found"
        lines.append(f"• *{a['company']}* ({a['headcount_range'] or '?'} people) {a['posts']} post(s), latest "
                     f"{a['latest_post']} <{a['job_url']}|job> · owner: {owners}")
        if a.get("opener"):
            lines.append(f"   _Opener:_ {a['opener']}")
    if len(accts) > limit:
        lines.append(f"…and {len(accts) - limit} more in out/accounts.csv")
    return "\n".join(lines)


def write_outputs(result: Dict[str, Any], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "accounts.json"), "w") as fh:
        json.dump({"summary": summary(result), **result}, fh, indent=1)
    with open(os.path.join(out_dir, "accounts.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "domain", "headcount_range", "posts", "latest_post", "job_url", "titles",
                    "owner_names", "owner_titles", "owner_profiles", "opener"])
        for a in result["accounts"]:
            w.writerow([a["company"], a["domain"], a["headcount_range"], a["posts"], a["latest_post"], a["job_url"],
                        "; ".join(a["titles"]), "; ".join(o["name"] for o in a["owners"]),
                        "; ".join(o["title"] for o in a["owners"]), "; ".join(o["profile_url"] for o in a["owners"]),
                        a.get("opener", "")])


def only_new(accounts: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    return [a for a in accounts if f"{a['company_id']}:{a['job_url']}" not in seen]


def save_state(accounts: List[Dict[str, Any]], state_path: str) -> None:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    seen.update(f"{a['company_id']}:{a['job_url']}" for a in accounts)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def split(value: str) -> List[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Hiring Signal Account Alerts (Crustdata data layer)")
    p.add_argument("--roles", default=os.environ.get("ROLES", DEFAULT_ROLES), help="exact job titles, comma list")
    p.add_argument("--owner-titles", default=os.environ.get("OWNER_TITLES", DEFAULT_OWNERS))
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "30")))
    p.add_argument("--country", default=os.environ.get("COUNTRY", "United States"), help='"" for worldwide')
    p.add_argument("--headcount", default=os.environ.get("HEADCOUNT", DEFAULT_HEADCOUNT), help='e.g. "11-50,51-200"; "" for any')
    p.add_argument("--max-jobs", type=int, default=500)
    p.add_argument("--no-owners", action="store_true", help="skip the stack-owner lookup")
    p.add_argument("--openers", action="store_true", help="draft a first line per account with Claude")
    p.add_argument("--dry-run", action="store_true", help="never send to Slack")
    p.add_argument("--all", action="store_true", help="include accounts already alerted on")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    roles, owners = split(args.roles), split(args.owner_titles)
    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    client = Crustdata()
    jobs = list(client.job_search(job_filters(roles, since, args.country or None, split(args.headcount)), JOB_FIELDS,
                                  max_results=args.max_jobs, sorts=[{"field": "metadata.date_added", "order": "desc"}]))
    result = {"posts": len(jobs), **group_accounts(jobs)}
    if not args.all:
        result["accounts"] = only_new(result["accounts"], args.state)
    if not args.no_owners and result["accounts"]:
        attach_owners(client, result["accounts"], owners)
    if args.openers:
        if not claude_available():
            print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): skipping openers", file=sys.stderr)
        else:
            for a in result["accounts"]:
                a["opener"] = ask_claude(
                    f"{a['company']} ({a['headcount_range']} people) just posted a {a['titles'][0] if a['titles'] else 'GTM'} role. "
                    "Write one short, specific first line (max 25 words) for a note to their RevOps lead about it. "
                    "No hype, no emojis. Return only the line.") or ""
    write_outputs(result, args.out)
    print(json.dumps(summary(result), indent=1))
    if result["accounts"]:
        print(send_slack(slack_text(result, roles, since), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(result["accounts"], args.state)
    else:
        print("No new hiring signals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
