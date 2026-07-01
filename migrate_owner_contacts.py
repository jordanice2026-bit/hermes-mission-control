#!/usr/bin/env python3
"""
migrate_owner_contacts.py — one-time cleanup.

Moves phone numbers + emails currently buried in owner Notes into the
structured Phone 1-5 / Email 1-3 fields, then strips them from Notes
(preserving any non-contact note text like "Co-owner: ...").

Idempotent: re-running is safe (already-migrated owners have clean notes).
Dry-run by default; pass --apply to write.
"""
import sys, re, asyncio, httpx
sys.path.insert(0, '/opt/data/mission-control')
import tc

APPLY = '--apply' in sys.argv

PHONE_RE = re.compile(r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE_FIELDS = ["Primary Phone", "Secondary Phone", "Phone 3", "Phone 4", "Phone 5"]
EMAIL_FIELDS = ["Primary Email", "Email 2", "Email 3"]


def norm_phone(p):
    return re.sub(r'\D', '', p)[-10:]


def rt(p): return ''.join(t.get('plain_text', '') for t in (p or {}).get('rich_text', []))
def ph(p): return (p or {}).get('phone_number') or ''
def em(p): return (p or {}).get('email') or ''
def title(p): return ''.join(t.get('plain_text', '') for t in (p or {}).get('title', []))


async def main():
    h = tc._notion_headers()
    async with httpx.AsyncClient(timeout=60) as c:
        # fetch all owners
        results, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = await c.post(f"https://api.notion.com/v1/data_sources/{tc.OWNERS_DS_ID}/query", headers=h, json=body)
            d = r.json()
            results.extend(d.get("results", []))
            if not d.get("has_more"):
                break
            cursor = d["next_cursor"]

        print(f"Scanning {len(results)} owners ({'APPLY' if APPLY else 'DRY-RUN'})\n")
        migrated = 0
        for pg in results:
            pr = pg['properties']
            name = title(pr.get('Owner Name'))
            notes = rt(pr.get('Notes'))

            # existing structured values
            struct_phones = [ph(pr.get(f)) for f in PHONE_FIELDS]
            struct_emails = [em(pr.get(f)) for f in EMAIL_FIELDS]

            notes_phones = PHONE_RE.findall(notes)
            notes_emails = EMAIL_RE.findall(notes)
            if not notes_phones and not notes_emails:
                continue  # nothing in notes to migrate

            # merge: structured first, then notes extras, dedup
            seen_p, phones = set(), []
            for p in [x for x in struct_phones if x] + notes_phones:
                k = norm_phone(p)
                if k and k not in seen_p:
                    seen_p.add(k); phones.append(p)
            seen_e, emails = set(), []
            for e in [x for x in struct_emails if x] + notes_emails:
                el = e.strip().lower()
                if el and el not in seen_e:
                    seen_e.add(el); emails.append(e.strip())

            phones = phones[:5]
            emails = emails[:3]

            # build clean notes: remove the phone/email lines, keep the rest
            clean_lines = []
            for line in notes.splitlines():
                low = line.lower()
                if low.startswith('extra phones') or low.startswith('emails (batchleads') or low.startswith('emails:'):
                    continue
                # also drop lines that are ONLY phone/email tokens
                stripped = PHONE_RE.sub('', line)
                stripped = EMAIL_RE.sub('', stripped).strip(' |,;-')
                if stripped:
                    clean_lines.append(line)
            clean_notes = "\n".join(clean_lines).strip()

            props = {}
            for i, fld in enumerate(PHONE_FIELDS):
                props[fld] = {"phone_number": phones[i] if i < len(phones) else None}
            for i, fld in enumerate(EMAIL_FIELDS):
                props[fld] = {"email": emails[i] if i < len(emails) else None}
            props["Notes"] = {"rich_text": ([{"text": {"content": clean_notes}}] if clean_notes else [])}

            print(f"• {name[:32]:32s} phones={len(phones)} emails={len(emails)} | notes: {repr(notes[:40])} -> {repr(clean_notes[:40])}")

            if APPLY:
                for attempt in range(4):
                    rr = await c.patch(f"https://api.notion.com/v1/pages/{pg['id']}", headers=h, json={"properties": props})
                    if rr.status_code < 300:
                        break
                    await asyncio.sleep(3)
                else:
                    print(f"    ⚠️ FAILED: {rr.text[:150]}")
                    continue
            migrated += 1

        print(f"\n{'Migrated' if APPLY else 'Would migrate'} {migrated} owner(s).")
        if not APPLY:
            print("Re-run with --apply to write changes.")


if __name__ == '__main__':
    asyncio.run(main())
