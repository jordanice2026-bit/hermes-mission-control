#!/usr/bin/env python3
"""One-shot: create the Listings database in Notion under the root page.

Listings are the home for a property from the moment the listing is won,
BEFORE any purchase agreement exists. When both the listing agreement and
purchase agreement are fully executed, the listing auto-promotes into Deals.
"""
import sys, json, httpx, asyncio
sys.path.insert(0, '/opt/data/mission-control')
import tc

ROOT_PAGE = '38f9925f-e691-819d-9f93-fe461117ed82'

# Standard 30-day transaction checklist (key contract deadlines only)
CHECKLIST_ITEMS = [
    'Earnest money delivered',
    'Inspection completed',
    'Inspection response deadline',
    'Appraisal ordered',
    'Financing / loan commitment',
    'Title ordered',
    'Title commitment reviewed',
    'Final walkthrough',
    'Closing',
]

PROPS = {
    'Property Address': {'title': {}},
    'Listing Status': {'select': {'options': [
        {'name': 'Pre-Listing', 'color': 'gray'},
        {'name': 'Active', 'color': 'green'},
        {'name': 'Pending', 'color': 'yellow'},
        {'name': 'Under Contract', 'color': 'orange'},
        {'name': 'Sold', 'color': 'blue'},
        {'name': 'Withdrawn', 'color': 'red'},
        {'name': 'Expired', 'color': 'brown'},
    ]}},
    # Listing agreement terms (parsed)
    'List Price': {'number': {'format': 'dollar'}},
    'Commission Pct': {'number': {'format': 'percent'}},
    'Listing Type': {'select': {'options': [
        {'name': 'Exclusive Right to Sell', 'color': 'green'},
        {'name': 'Exclusive Agency', 'color': 'blue'},
        {'name': 'Open', 'color': 'gray'},
    ]}},
    'Property Type': {'select': {'options': [
        {'name': 'Single Family (1 unit)', 'color': 'gray'},
        {'name': 'Duplex (2 unit)', 'color': 'blue'},
        {'name': 'Triplex (3 unit)', 'color': 'purple'},
        {'name': 'Fourplex (4 unit)', 'color': 'pink'},
    ]}},
    'Listing Start Date': {'date': {}},
    'Listing Expiration': {'date': {}},
    'Seller Names': {'rich_text': {}},
    # Documents (Notion native file uploads)
    'Listing Agreement': {'files': {}},
    'Listing Agreement Executed': {'checkbox': {}},
    'Purchase Agreement': {'files': {}},
    'Purchase Agreement Executed': {'checkbox': {}},
    # Automation
    'Promoted to Deal': {'checkbox': {}},
    'Promoted Deal ID': {'rich_text': {}},
    # Transaction checklist (JSON string of {item, done, date} once PA attached)
    'Checklist': {'rich_text': {}},
    'MLS Number': {'rich_text': {}},
    'Notes': {'rich_text': {}},
    # Relations
    'Owner': {'relation': {'data_source_id': tc.OWNERS_DS_ID, 'type': 'single_property',
                           'single_property': {}}},
}


async def main():
    h = tc._notion_headers()
    body = {
        'parent': {'type': 'page_id', 'page_id': ROOT_PAGE},
        'title': [{'type': 'text', 'text': {'content': '📋 Listings'}}],
        'is_inline': False,
        'initial_data_source': {'properties': PROPS},
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post('https://api.notion.com/v1/databases', headers=h, json=body)
        if r.status_code >= 300:
            print('ERROR', r.status_code, r.text)
            return
        d = r.json()
        db_id = d['id']
        ds = d.get('data_sources') or []
        ds_id = ds[0]['id'] if ds else None
        print('LISTINGS_DB_ID =', db_id)
        print('LISTINGS_DS_ID =', ds_id)
        print('CHECKLIST_ITEMS =', json.dumps(CHECKLIST_ITEMS))

asyncio.run(main())
