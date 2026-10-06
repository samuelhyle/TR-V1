# Tier 0 licensing email — draft, not sent

**Status update, 1 October 2026:** the owner confirms separate existing written
permission for focused Tori collection. Private operator-confirmed scope metadata
is recorded in `data/authorizations/tori_collection.json`. This historical request
draft is not the permission record. Before adapting it for additional rights,
verify its personal/commercial description and claims about a published notice,
completed LIA and counsel opinion against the actual project status.

The single highest-value action in the project (`PROJECT_PLAN.md`:321, `:413`).
Everything else here is fallback. If this is answered, the crawler stops being
the acquisition strategy.

## Before sending — two things to fill in

1. **Recipient.** `PROJECT_PLAN.md`:155 names `tuki.tori.fi`, which is a
   hostname, not an address, and it is a *secondhand* reading of the terms. Use
   the real address from Tori-kauppa's business surface (`/yrityksille/`), or the
   support route. Do not guess it.
2. **Sender.** Must be the same address as `TORI_CONTACT_EMAIL`, so that a reply
   reaches the mailbox the User-Agent already advertises to the platform.

## Subject

Research data request — Finnish second-hand market study (individual researcher)

## Body

Subject: Research data request — Finnish second-hand market study

Hello,

I am an individual researcher measuring the Finnish second-hand market — ask
prices, how long listings take to sell, and the unit economics of reselling. This
is a personal research project, not a business, and I am not a competitor of
Tori or of any seller on it.

**What I am asking for, in order of preference:**

1. **An aggregated market-price feed** — category and region level, over time.
2. **A category-level sales aggregate** — units sold per category per period.
   I understand Tori-kauppa §13.3 already contemplates sharing data of that shape
   for data "publicly visible in the Service"; I would like to do that properly
   rather than approximately. *(I have read the terms second-hand and may have
   that clause wrong — please correct me.)*
3. **If neither is possible: written permission** for a named, scoped research
   crawl, so I am collecting under your terms rather than my own reading of them.

**What I am not asking for, and will not accept:** seller names, seller ids,
contact details, postal codes, precise coordinates, messaging, or any
person-level data. Municipality is the coarsest location Tori publishes and it is
the finest I store. If a feed can only be provided at that grain, that is
sufficient.

**What I will do with it:** aggregate statistics only. I will not resell it,
license it onward, or publish a public price-comparison frontend — a
publicly available meta-search interface over a marketplace database is exactly
the thing I am trying not to build, so the output is internal analysis.

**If the answer is no, or the answer is that I should not be collecting at all,
please just say so and I will stop.** I would much rather have a clear no than
continue and have you find out later.

For context on what compliance already looks like in what I have built, so you
can judge whether this is a reasonable risk to permit:

- Every request carries a descriptive User-Agent with a real, monitored contact
  address — `TORI_CONTACT_EMAIL`, the same address this email comes from.
- One request per second, no parallelism, exponential backoff on any 429 or 5xx,
  and an immediate stop on the first 429 rather than a retry.
- `robots.txt` is enforced in code, not policy, with no override switch. The
  disallowed account, messaging, favorites and contact surfaces are never
  requested; only the public search and listing surface is.
- Item pages are never retained on disk, because they embed seller identity and
  precise coordinates. My parser builds an explicit allowlist and discards the
  payload, so there is no code path that could store those fields.
- Documented retention limits, enforced on every run, not just written down.
- A GDPR Article 14 notice with a named controller and a working opt-out, so
  anyone who spots the collector can reach me and have their listing dropped.
- A legitimate-interest assessment, and a separate counsel opinion on it.

If there is a partnerships, data-protection, or business-data route that this
should go through instead of support, please point me at it and I will take it
there.

Thank you for your time.

Kind regards,
[your name]
[the TORI_CONTACT_EMAIL address]

## After sending

Record the outcome. `PROJECT_PLAN.md`:413 — "buying the data when it's offered"
is what separates a project that can be defended from one that has to be
apologised for. A "no" is a real answer and should be logged as one, because it
is also the trigger to escalate from crawling to the purchase ledger, which is
the asset no platform can revoke.

## Two things to fix separately

- `legal/LIA.md`:8 and `legal/ART14_NOTICE.md`:6 both list
  `research@tori.fi` as the contact for data subject requests. That is the
  platform's own domain, not ours, so an Article 14 notice currently points
  data subjects at the wrong party. Both need the real controller address and a
  named legal entity.
- Both of those documents are also still outstanding per `cli compliance`, which
  is the more substantive gap.
