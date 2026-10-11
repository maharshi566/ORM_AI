# The morning briefing

Every morning ORM_AI tells each shop owner what needs attention, without being asked:

```text
Good morning Ramesh. Ramesh Kirana Store, Wednesday 30 Sep 2026:
- Low on stock: 4 products, 2 not yet ordered. Toor Dal 1kg (PRD-0003, 3 left); ...
- Overdue payments: 3 customers, Rs 7,420.00 in all. ...
- Late deliveries: PO-00585 from Shree Balaji Traders, 7 days late.
- Waiting for approval: 2.
- Yesterday: 41 bills, Rs 12,345.00.
```

| Section | Counted as | The same as the agents' tool |
| --- | --- | --- |
| Low on stock | Active products at or below their reorder level; "not yet ordered" when no open purchase order covers them | `get_low_stock_products` |
| Overdue payments | Customers whose oldest unpaid credit bill is past its due date (payments clear the oldest bill first) | `get_customer_account` |
| Late deliveries | Open purchase orders past their expected date | `get_purchase_orders` |
| Waiting for approval | Actions a person still has to decide | the approvals inbox |
| Yesterday | Completed bills and their total | `get_sales_summary` |

The figures come from the same code the agents' tools use, so a briefing and a chat
answer always agree. No model is involved: it is counting, so it costs nothing and is
exact. Each briefing names up to five items per section; the counts cover everything.

## Where it shows

- **On the website**, above the chat (when a conversation is empty), with an "Ask about
  this" link on each section that starts the matching question.
- **On WhatsApp** to the owner, as the `daily_briefing` template
  ([whatsapp.md](whatsapp.md)): the counts, and a nudge to open ORM_AI for the details.
  WhatsApp templates have fixed wording, so the full list stays on the website.

A briefing is built once per shop and day and stored (`briefings`), the first time
anyone opens the chat that day or when the morning run starts, whichever comes first.
Running the morning run twice sends nothing twice.

## Who gets it on WhatsApp

With `MESSAGING_PROVIDER=whatsapp`, each shop's owner, on the phone number in their user
record, when that number can be real. The synthetic owners' numbers are made up, so on
the demo data nobody gets it unless you list shops:

```env
BRIEFING_SHOPS=SHOP-001                 # only these shops get the WhatsApp briefing
WHATSAPP_TEST_RECIPIENT=+919876543210   # and it arrives on your phone
```

## Running it every morning

The morning run is `POST /api/briefings/run`, called with the header
`X-Cron-Secret: <BRIEFING_CRON_SECRET>` (or by an admin). Pick one way to call it:

- **GitHub Actions** (free, nothing to host): `.github/workflows/briefing.yml` runs at
  7:30 IST. In the repository's **Settings → Secrets and variables → Actions**, add
  `BRIEFING_URL` (the backend's public address) and `BRIEFING_CRON_SECRET` (the same
  value as on the backend). **Actions → Morning briefing → Run workflow** tries it at
  once. GitHub may start scheduled jobs a few minutes late.
- **cron on your own server**:

  ```text
  30 7 * * *  cd /srv/orm_ai && docker compose -f docker-compose.prod.yml exec -T backend python -m scripts.briefing --all
  ```

Make the secret with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## By hand

```powershell
python -m scripts.briefing --shop SHOP-001   # print today's briefing
python -m scripts.briefing --all             # build every shop's, and send where set up
```

`GET /api/briefings/today?shop_id=SHOP-001` returns today's briefing as JSON (a shop
user gets their own shop's).
