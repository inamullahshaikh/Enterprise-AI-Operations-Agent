# Renewal Playbook

This playbook describes how the Northstar Analytics customer success team handles
subscriptions that are renewing soon.

## Who this applies to

Any account whose subscription end date falls within the current calendar month. Check the
`subscriptions` table for `end_date` and `auto_renew` status before doing anything else — an
account with `auto_renew = true` still gets a check-in, but the urgency is lower.

## Usage drop threshold

If an account's product usage has dropped more than 30% compared to the previous month, treat
the renewal as at risk regardless of MRR. Pull `usage_daily` for both months, sum active users
per account, and compare.

## MRR threshold for a white-glove outreach

Accounts with monthly recurring revenue (MRR) above $500 get a personal check-in email from
their account owner before the renewal date. Accounts below that threshold get an automated
renewal reminder instead — the manual outreach isn't worth the account team's time for a small
account, even if usage dropped.

## Escalation steps

1. Confirm the renewal date and current MRR from the `subscriptions` table.
2. Check the usage trend for the last two months.
3. If MRR is above $500 **and** usage dropped more than 30%, escalate to the account owner
   (listed in `accounts.owner_email`) with a summary of the usage drop.
4. If MRR is above $500 but usage is stable, send a standard renewal check-in.
5. If MRR is at or below $500, no manual escalation — an automated reminder is enough.
6. Never promise a discount or contract change in an automated message; that always requires a
   human on the account team to approve first.

## Contacts

Escalations go to the account owner listed on the account record, not to a shared inbox. If the
account owner is unclear, check with the sales operations lead before sending anything external.
