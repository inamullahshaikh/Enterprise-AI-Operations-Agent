# Support Escalation Policy

How Northstar Analytics support tickets get triaged and escalated.

## Severity levels

- **Sev 1** — production outage or data loss for the customer. Page the on-call engineer
  immediately; do not wait for a human to review the ticket first.
- **Sev 2** — a major feature is broken but there's a workaround. Response target: 4 business
  hours.
- **Sev 3** — a minor bug or a question. Response target: 2 business days.

## When to escalate a ticket to the account owner

Escalate a support ticket to the account's owner (not just the support queue) when any of these
are true:

- The account's MRR is above $500 and the ticket is Sev 1 or Sev 2.
- The same account has opened three or more tickets in the last 30 days.
- The customer explicitly asks to speak with their account manager.

## What "resolved" means

A ticket is only marked resolved once the customer confirms the issue is fixed, not when the
support engineer believes it's fixed. If the customer hasn't responded within 5 business days
of a proposed fix, the ticket can be closed as resolved-no-response, but it must be reopened
immediately if the customer follows up afterward.

## Data handling in tickets

Never paste a customer's raw database credentials or API keys into a support ticket, even when
troubleshooting — redact them first. If a ticket already contains unredacted credentials, treat
that as a Sev 2 security issue on its own and escalate it to the security channel, independent
of whatever the original ticket was about.
