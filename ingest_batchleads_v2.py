#!/usr/bin/env python3
"""
BatchLeads v2 Excel → Notion Ingestion
37-column sheet with APN, property type, beds/baths, sqft,
assessed value, equity, MLS status, loan data, etc.
"""
import sys, os, json, re, urllib.request, time
from datetime import date

sys.path.insert(0, '/opt/data/pylibs')
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
NOTION_KEY   = os.environ["NOTION_API_KEY"]
OWNERS_DB_ID = "af076d45-42d5-42a1-9bc6-8d9471c31530"
PROPS_DB_ID  = "2c3885ba-bf8d-4e11-aaa3-30f40bf011af"
OWNERS_DS_ID = "d215a50d-ec81-457c-808b-cd9be5ee3b9a"
PROPS_DS_ID  = "c113e472-dbe1-42c2-91cd-ada616e520d2"
EXCEL_PATH   = "/opt/hermes/.hermes/desktop-attachments/BatchLeads_Properties_v2.xlsx"
TODAY        = date.today().isoformat()

H = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2025-09-03",
    "Content-Type": "application/json"
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def notion_req(method, endpoint, payload=None):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{endpoint}",
        data=data, headers=H, method=method
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Exception(f"HTTP {e.code} [{endpoint}]: {e.read().decode()[:400]}")

def clean(val, fallback=""):
    """Return stripped string or fallback if nan/dash/empty."""
    s = str(val).strip()
    return fallback if s in ("nan", "-", "", "None") else s

def parse_phones(raw):
    """Extract + normalize all phone numbers from BatchLeads concatenated string."""
    if not raw or clean(raw) == "":
        return []
    phones = re.findall(r'\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}', str(raw))
    out = []
    for p in phones:
        d = re.sub(r'\D', '', p)
        if len(d) == 10:
            out.append(f"({d[:3]}) {d[3:6]}-{d[6:]}")
    return out

def parse_emails(raw):
    if not raw or clean(raw) == "":
        return []
    return [e.strip() for e in str(raw).split() if '@' in e]

def parse_dollar(val):
    """'$94,000' → 94000.0, '-' → None"""
    s = clean(val)
    if not s:
        return None
    s = re.sub(r'[$,]', '', s)
    try:
        return float(s)
    except ValueError:
        return None

def parse_pct(val):
    """'33.9%' → 33.9, '-' → None"""
    s = clean(val)
    if not s:
        return None
    s = s.replace('%', '').strip()
    try:
        return float(s)
    except ValueError:
        return None

def parse_int(val):
    try:
        v = int(float(str(val)))
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None

def normalize_owner(fname, lname):
    """Return (display_name, contact_type)."""
    f, l = clean(fname), clean(lname)
    if not f or f == '-':
        name = l.title()
    else:
        name = f"{f.title()} {l.title()}".strip()
    lu = (f + " " + l).upper()
    if 'TRUST' in lu:
        ctype = 'Trust'
    elif any(x in lu for x in ['LLC','L.L.C','INC','CORP','LTD','HOLDINGS','PROPERTIES','GROUP','PARTNERS']):
        ctype = 'LLC'
    else:
        ctype = 'Individual'
    return name, ctype

# ── Ensure Properties DS has all new fields ───────────────────────────────────
print("📐 Patching Properties schema with v2 fields...")
notion_req("PATCH", f"data_sources/{PROPS_DS_ID}", {
    "properties": {
        "Property Type": {
            "select": {
                "options": [
                    {"name": "Multi-Family Dwelling"},
                    {"name": "Triplex"},
                    {"name": "Duplex"},
                    {"name": "SFR"},
                    {"name": "Commercial"},
                    {"name": "Land"},
                    {"name": "Other"}
                ]
            }
        },
        "MLS Status": {
            "select": {
                "options": [
                    {"name": "OFF MARKET"},
                    {"name": "SOLD"},
                    {"name": "ACTIVE"},
                    {"name": "PENDING"}
                ]
            }
        },
        "Bedrooms":       {"number": {"format": "number"}},
        "Bathrooms":      {"number": {"format": "number"}},
        "Sq Ft":          {"number": {"format": "number"}},
        "Lot Size":       {"number": {"format": "number"}},
        "Year Built":     {"number": {"format": "number"}},
        "Assessed Value": {"number": {"format": "dollar"}},
        "Est. Value":     {"number": {"format": "dollar"}},
        "Est. Equity":    {"number": {"format": "dollar"}},
        "Est. LTV":       {"number": {"format": "percent"}},
        "Last Sale Date": {"date": {}},
        "Last Sale Amount": {"number": {"format": "dollar"}},
        "Loan Type":      {"rich_text": {}},
        "Loan Interest Rate": {"number": {"format": "percent"}},
        "Total Loan Balance": {"number": {"format": "dollar"}},
        "APN":            {"rich_text": {}},
    }
})
print("   ✅ Properties schema updated")

# ── Load Excel ────────────────────────────────────────────────────────────────
print(f"\n📂 Loading v2 Excel...")
df = pd.read_excel(EXCEL_PATH)
print(f"   {len(df)} rows, {len(df.columns)} columns")

# ── Build owner-centric structure ─────────────────────────────────────────────
owners = {}
for _, row in df.iterrows():
    name, ctype = normalize_owner(row['Owner First Name'], row['Owner Last Name'])
    phones = parse_phones(row['Phone Numbers'])
    emails = parse_emails(row['Emails'])

    # Owner 2 (co-owner)
    o2f, o2l = clean(row.get('Owner 2 First Name', '')), clean(row.get('Owner 2 Last Name', ''))
    owner2 = ""
    if o2f and o2f != '-' and o2l and o2l != '-' and f"{o2f} {o2l}" != f"{clean(row['Owner First Name'])} {clean(row['Owner Last Name'])}":
        owner2 = f"{o2f.title()} {o2l.title()}"

    if name not in owners:
        owners[name] = {
            "name": name,
            "contact_type": ctype,
            "owner2": owner2,
            "primary_phone": phones[0] if phones else "",
            "secondary_phone": phones[1] if len(phones) > 1 else "",
            "extra_phones": phones[2:],
            "emails": emails,
            "mailing_address": clean(row['Mailing Address']),
            "mailing_city": clean(row['Mailing City']),
            "mailing_state": clean(row['Mailing State']),
            "mailing_zip": clean(row['Mailing Zip Code']),
            "county": clean(row['County'], "Madison"),
            "properties": []
        }

    # Parse property financials
    def parse_sqft_str(val):
        s = clean(val)
        if not s: return None
        s = s.replace(',', '')
        try: return int(float(s))
        except: return None

    def parse_lot(val):
        s = clean(val)
        if not s: return None
        s = s.replace(',', '')
        try: return int(float(s))
        except: return None

    # Last sale date
    last_sale_raw = clean(row.get('Last Sale Date', ''))
    last_sale_date = None
    if last_sale_raw:
        try:
            from datetime import datetime
            last_sale_date = datetime.strptime(last_sale_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
        except:
            pass

    owners[name]["properties"].append({
        "address":        f"{clean(row['Property Address'])}, {clean(row['City'])}, {clean(row['State'])} {clean(row['Zip'])}",
        "city":           clean(row['City']),
        "state":          clean(row['State']),
        "zip":            clean(row['Zip']),
        "county":         clean(row['County'], "Madison"),
        "apn":            clean(row['APN']),
        "prop_type":      clean(row['Property Type']),
        "beds":           parse_int(row.get('Bedrooms')),
        "baths":          parse_int(row.get('Bathrooms')),
        "sqft":           parse_sqft_str(row.get('Property Sqft')),
        "lot_size":       parse_lot(row.get('Lot Size')),
        "year_built":     parse_int(row.get('Year Built')),
        "assessed_val":   parse_dollar(row.get('Assessed Value')),
        "est_value":      parse_dollar(row.get('Est. Value')),
        "est_equity":     parse_dollar(row.get('Est. Equity')),
        "est_ltv":        parse_pct(row.get('Est. LTV')),
        "last_sale_date": last_sale_date,
        "last_sale_amt":  parse_dollar(row.get('Last Sale Amount')),
        "loan_type":      clean(row.get('Loan Type', '')),
        "loan_rate":      parse_pct(row.get('Loan Interest Rate')),
        "loan_balance":   parse_dollar(row.get('Total Loan Balance')),
        "mls_status":     clean(row.get('MLS Status', '')),
    })

print(f"   {len(owners)} unique owners\n")

# ── Write Owners ──────────────────────────────────────────────────────────────
owner_page_ids = {}
print("👤 Creating Owners...")
for owner_name, owner in owners.items():
    props = {
        "Owner Name":          {"title": [{"text": {"content": owner_name}}]},
        "Contact Type":        {"select": {"name": owner["contact_type"]}},
        "Outreach Stage":      {"select": {"name": "New Lead"}},
        "Verification Status": {"select": {"name": "Pending"}},
        "Phone Verified":      {"checkbox": False},
        "Do Not Contact":      {"checkbox": False},
        "Data Source":         {"select": {"name": "BatchLeads"}},
        "County":              {"select": {"name": owner["county"]}},
        "Date Added":          {"date": {"start": TODAY}},
    }
    # Phones -> structured Phone 1-5 fields (NEVER notes)
    all_phones = []
    if owner["primary_phone"]:   all_phones.append(owner["primary_phone"])
    if owner["secondary_phone"]: all_phones.append(owner["secondary_phone"])
    all_phones.extend(owner.get("extra_phones", []))
    # dedupe preserving order, cap at 5
    seen_p = set(); phones = []
    for p in all_phones:
        key = re.sub(r'\D', '', p)[-10:]
        if key and key not in seen_p:
            seen_p.add(key); phones.append(p)
    phone_fields = ["Primary Phone", "Secondary Phone", "Phone 3", "Phone 4", "Phone 5"]
    for i, fld in enumerate(phone_fields):
        if i < len(phones):
            props[fld] = {"phone_number": phones[i]}

    # Emails -> structured Primary Email / Email 2 / Email 3 (NEVER notes)
    seen_e = set(); emails = []
    for e in owner.get("emails", []):
        el = e.strip().lower()
        if el and el not in seen_e:
            seen_e.add(el); emails.append(e.strip())
    email_fields = ["Primary Email", "Email 2", "Email 3"]
    for i, fld in enumerate(email_fields):
        if i < len(emails):
            props[fld] = {"email": emails[i]}

    if owner["mailing_address"]:
        props["Mailing Address"] = {"rich_text": [{"text": {"content": owner["mailing_address"]}}]}
    if owner["mailing_city"]:
        props["Mailing City"]  = {"rich_text": [{"text": {"content": owner["mailing_city"]}}]}
    if owner["mailing_state"]:
        props["Mailing State"] = {"rich_text": [{"text": {"content": owner["mailing_state"]}}]}
    if owner["mailing_zip"]:
        props["Mailing Zip"]   = {"rich_text": [{"text": {"content": owner["mailing_zip"]}}]}

    # Notes: co-owner only — phones + emails now live in structured fields
    notes = []
    if owner["owner2"]:
        notes.append(f"Co-owner: {owner['owner2']}")
    if notes:
        props["Notes"] = {"rich_text": [{"text": {"content": "\n".join(notes)}}]}

    page = notion_req("POST", "pages", {"parent": {"database_id": OWNERS_DB_ID}, "properties": props})
    owner_page_ids[owner_name] = page["id"]
    print(f"   ✅ {owner_name} [{owner['contact_type']}] → {page['id']}")

print(f"\n✅ {len(owner_page_ids)} owners created\n")

# ── Write Properties ───────────────────────────────────────────────────────────
print("🏠 Creating Properties...")
prop_count = 0
for owner_name, owner in owners.items():
    owner_pid = owner_page_ids[owner_name]
    for prop in owner["properties"]:
        p = {
            "Property Address": {"title": [{"text": {"content": prop["address"]}}]},
            "Owner":            {"relation": [{"id": owner_pid}]},
            "County":           {"select": {"name": prop["county"] or "Madison"}},
            "Data Source":      {"select": {"name": "BatchLeads"}},
            "Date Added":       {"date": {"start": TODAY}},
        }
        if prop["apn"]:
            p["APN"] = {"rich_text": [{"text": {"content": prop["apn"]}}]}
        if prop["prop_type"]:
            p["Property Type"] = {"select": {"name": prop["prop_type"]}}
        if prop["mls_status"]:
            p["MLS Status"] = {"select": {"name": prop["mls_status"]}}
        if prop["beds"] is not None:
            p["Bedrooms"] = {"number": prop["beds"]}
        if prop["baths"] is not None:
            p["Bathrooms"] = {"number": prop["baths"]}
        if prop["sqft"] is not None:
            p["Sq Ft"] = {"number": prop["sqft"]}
        if prop["lot_size"] is not None:
            p["Lot Size"] = {"number": prop["lot_size"]}
        if prop["year_built"] is not None:
            p["Year Built"] = {"number": prop["year_built"]}
        if prop["assessed_val"] is not None:
            p["Assessed Value"] = {"number": prop["assessed_val"]}
        if prop["est_value"] is not None:
            p["Est. Value"] = {"number": prop["est_value"]}
        if prop["est_equity"] is not None:
            p["Est. Equity"] = {"number": prop["est_equity"]}
        if prop["est_ltv"] is not None:
            p["Est. LTV"] = {"number": prop["est_ltv"] / 100}  # Notion percent = 0–1
        if prop["last_sale_date"]:
            p["Last Sale Date"] = {"date": {"start": prop["last_sale_date"]}}
        if prop["last_sale_amt"] is not None:
            p["Last Sale Amount"] = {"number": prop["last_sale_amt"]}
        if prop["loan_type"]:
            p["Loan Type"] = {"rich_text": [{"text": {"content": prop["loan_type"]}}]}
        if prop["loan_rate"] is not None:
            p["Loan Interest Rate"] = {"number": prop["loan_rate"] / 100}
        if prop["loan_balance"] is not None:
            p["Total Loan Balance"] = {"number": prop["loan_balance"]}

        page = notion_req("POST", "pages", {"parent": {"database_id": PROPS_DB_ID}, "properties": p})
        print(f"   ✅ {prop['address'][:60]} → {page['id']}")
        prop_count += 1
        time.sleep(0.15)

print(f"\n✅ {prop_count} properties created\n")

# ── Patch back-relations on Owners ────────────────────────────────────────────
print("🔗 Patching Owner ↔ Property back-relations...")
props_result = notion_req("POST", f"data_sources/{PROPS_DS_ID}/query", {"page_size": 100})
owner_to_props = {}
for page in props_result.get("results", []):
    for rel in page["properties"].get("Owner", {}).get("relation", []):
        oid = rel["id"]
        owner_to_props.setdefault(oid, []).append(page["id"])

for oid, pids in owner_to_props.items():
    notion_req("PATCH", f"pages/{oid}", {
        "properties": {"Properties": {"relation": [{"id": pid} for pid in pids]}}
    })

print(f"   ✅ Back-relations set for {len(owner_to_props)} owners")

print(f"\n{'='*60}")
print(f"🎉 INGESTION COMPLETE")
print(f"   Owners:     {len(owner_page_ids)}")
print(f"   Properties: {prop_count}")
