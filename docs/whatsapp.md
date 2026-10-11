# WhatsApp: real messages and login codes

ORM_AI sends four kinds of WhatsApp messages through Meta's **WhatsApp Cloud API**:

| Purpose | Sent when | Template category |
| --- | --- | --- |
| Payment reminder | A person approves a reminder the agents proposed | Utility |
| Supplier follow-up | A person approves a message about a late or short delivery | Utility |
| Login code | Someone logs in with "Code on my phone" | Authentication |
| Morning briefing | Every morning, to each shop owner ([briefing.md](briefing.md)) | Utility |

Until you set it up, nothing leaves the backend (`MESSAGING_PROVIDER=mock`): the actions
still run and are recorded, and login codes appear in the backend log on your own
computer.

## How it stays safe

- **Approved templates only.** A business may start a WhatsApp conversation only with a
  template Meta has approved. ORM_AI fills in the template's variables; it never sends
  free text a model wrote.
- **No made-up numbers.** The synthetic data's phone numbers (`+91-00000-…`) cannot be
  real, and ORM_AI refuses to send to them (`app/core/phones.py`).
- **A test phone.** With `WHATSAPP_TEST_RECIPIENT` set, every message goes to that one
  number instead of the real recipient. Use it with the demo data: approve a reminder
  to a made-up customer and it arrives on your phone.
- **Every message still needs a person.** Reminders and supplier messages are actions;
  they wait for approval exactly as before ([agents.md](agents.md), section 9).
- **Signed receipts.** Delivery receipts must carry Meta's signature for your app
  (`WHATSAPP_APP_SECRET`); anything else is refused.

## Set it up (about 30 minutes, plus template approval)

You need a Facebook account and a phone number that is **not** already on WhatsApp (or
use the free test number Meta offers).

1. **Create the app.** At <https://developers.facebook.com/apps>, **Create app** →
   **Other** → **Business** → give it a name. On the app's page, add the **WhatsApp**
   product. Meta creates a WhatsApp Business Account and a free **test number**.
2. **Note two IDs.** WhatsApp → **API Setup** shows the **Phone number ID** (a long number,
   not the phone number itself). Put it in `.env` as `WHATSAPP_PHONE_NUMBER_ID`.
3. **Make a permanent token.** The token on API Setup expires in 24 hours. For a lasting
   one: Meta **Business Settings** → **Users** → **System users** → **Add** (admin) →
   **Add assets** → your app (full control) and your WhatsApp account → **Generate new
   token**, with `whatsapp_business_messaging` and `whatsapp_business_management`. Put it
   in `.env` as `WHATSAPP_ACCESS_TOKEN`. Treat it like a password.
4. **Allow test recipients.** While the app is in development mode, Meta delivers only to
   up to five numbers you add under API Setup → **To**. Add your own phone.
5. **Create the templates** below in **WhatsApp Manager** → **Message templates** →
   **Create template**. Approval usually takes minutes to a day.
6. **Switch it on** in `.env`:

   ```env
   MESSAGING_PROVIDER=whatsapp
   OTP_CHANNEL=whatsapp
   WHATSAPP_TEST_RECIPIENT=+919876543210   # your phone, while trying it
   ```

   Restart the backend. `APP_ENV=production` refuses to start if the token or the phone
   number ID is missing.

## The templates

Create each with the **name**, **category** and **language** below (English: `en`; set
`WHATSAPP_LANGUAGE` if you choose another). Type the body exactly, then give the sample
values Meta asks for. The variables must stay in this order: ORM_AI fills them by number.
Different names work too; set them in `WHATSAPP_TEMPLATE_*`.

### `payment_reminder` (Utility)

```text
Namaste {{1}}, a gentle reminder from {{2}}: Rs {{3}} is pending on your account since {{4}}. Please pay when convenient. Thank you.
```

Samples: `Asha Kulkarni`, `Ramesh Kirana Store`, `694.00`, `12 Sep 2026`.

### `supplier_follow_up` (Utility)

```text
Namaste {{1}}, this is {{2}}. About our order {{3}}: {{4}} Thank you.
```

Samples: `Mahesh`, `Ramesh Kirana Store`, `PO-00585`, `it was due on 23 Sep 2026 and has not
arrived yet (7 days late). Please share the delivery date.`

### `login_code` (Authentication)

Choose **Authentication**, then **Copy code** as the button. Meta writes the text itself
("*{{1}} is your verification code.*"); you can add the security note and the expiry
(10 minutes). ORM_AI sends the code as the body variable and as the button's value.

### `daily_briefing` (Utility)

```text
Good morning {{1}}. Today at {{2}}: {{3}} products low on stock, {{4}} customers with overdue payments, {{5}} late deliveries and {{6}} approvals waiting. Open ORM_AI for the details.
```

Samples: `Ramesh`, `Ramesh Kirana Store`, `4`, `3`, `1`, `2`.

## Delivery receipts

With the webhook connected, each message's row in `notifications` moves from `sent` to
`delivered` and `read` (or `failed`, with Meta's reason in the log). A status never goes
backwards, and a receipt Meta sends twice changes nothing.

1. Choose any long random string as the verify token and put it in `.env` as
   `WHATSAPP_VERIFY_TOKEN`. Copy your app's **App secret** (App settings → Basic) into
   `WHATSAPP_APP_SECRET`. Restart the backend.
2. In the app's WhatsApp → **Configuration** → **Webhook** → **Edit**: callback URL
   `https://<your backend>/api/webhooks/whatsapp`, and the same verify token. Meta calls
   the URL once to check it.
3. Under **Webhook fields**, subscribe to **messages**.

The webhook needs a public HTTPS address, so it works on a deployed server
([deploy.md](deploy.md)), not on `localhost`.

## When something goes wrong

ORM_AI turns Meta's errors into plain sentences; the token never appears in them.

| Message | What to do |
| --- | --- |
| "WhatsApp rejected the access token" | The token expired or lost its permissions: make a System User token (step 3). |
| "WhatsApp refused the template …" | The template does not exist, is not approved yet, or has a different number of variables. Compare it with the text above. |
| "This number is not on the test list" | In development mode, add the number under API Setup → To (up to five). |
| "The number on record … cannot receive WhatsApp messages" | The customer's number is made up or malformed. Fix it, or set `WHATSAPP_TEST_RECIPIENT` while testing. |
| "WhatsApp is busy or rate-limiting" | Temporary; the action can be retried. |

## Costs

Meta charges per delivered template message, by category and country; utility and
authentication messages to Indian numbers cost a fraction of a rupee each, and Meta's
pricing page has the current rates. Login codes and reminders are the bulk of the
volume; the morning briefing is one message per shop per day.
