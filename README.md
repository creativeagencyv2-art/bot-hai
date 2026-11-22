# Creative Agency Telegram Bot (FINAL v2)

Customer bot, 9 ready products, UPI top-up with automatic UTR verification, Telegram admin panel, live Silvex supplier.

## Fresh setup on Windows
1. Install Python 3.12 (terminal: winget install Python.Python.3.12), restart VS Code.
2. Open this folder in VS Code -> Terminal -> New Terminal.
3. py -3.12 -m venv venv
4. venv\Scripts\activate
5. pip install -r requirements.txt
6. copy .env.example .env   then fill .env
7. python main.py

## Updating an existing project
Copy everything from this folder into your project folder EXCEPT .env and venv. Replace files. Then run:
pip install -r requirements.txt
python main.py        (the database upgrades itself)

## First-time supplier setup (Telegram, as admin)
/api_check  ->  /rate 90  ->  /admin -> Sync Products  ->  check prices in Products & Plans.

## Payments
- Customer pays UPI, sends the 12-digit UTR.
- Approved automatically ONLY when the same UTR + exact amount exists as a real bank credit.
- Feed the bank credits in either way:
  1) Forward/paste the bank's credit SMS to the bot (admin) -> instant, no server needed.
  2) SMS-forwarder app on your phone -> POST https://YOUR-DOMAIN/webhook/sms with header X-Webhook-Secret
     (needs WEBHOOK_SECRET + BANK_SMS_SENDERS in .env and a public HTTPS address).
- Anything unclear (wrong amount, above AUTO_APPROVE_MAX_RS) waits for admin approval.

## Tests
python -m tests.test_wallet_orders
python -m tests.test_payments
python -m tests.test_silvex
python -m tests.test_catalog_autopay
python -m tests.test_bot_flow

## Sales channel
Create a Telegram channel, add the bot as ADMIN (permission: Post messages), put SALES_CHANNEL=@yourchannel in .env,
restart, then send /channel_test to the bot. After every purchase a post (product, price, masked buyer) appears there.

## Force join
Put FORCE_JOIN_CHANNELS=@yourchannel in .env (several: comma separated; private: -100123|https://t.me/+invite).
Add the bot as ADMIN of that channel. New users must join and tap "I've Joined" before they can use the bot. Admins are exempt.
