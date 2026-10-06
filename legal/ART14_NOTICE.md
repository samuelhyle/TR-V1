# [Company] — market information notice

**Controller:** [Company]
**Contact:** timanttiliiga666@gmail.com
**Version:** 0.3, 2 October 2026
**Status:** DRAFT — controller identity, approval and notice delivery pending.

This notice is prepared for controller review before publication. No publication
has been verified. The contact matches the collector configuration; the operator
must verify mailbox delivery and request handling before issuing the notice.

## Purpose and source

This workspace analyses publicly available Tori listings to understand asking
prices, listing durations and market conditions for internal second-hand
purchasing and resale decisions.

Discovery and detail checks focus nationwide on Pyöräily, Muu urheilu,
Tietotekniikka, Puhelimet ja tarvikkeet, Valokuvaus ja video, Videopelit ja
konsolit, and Ääni ja kuva, including their subcategories. A separate public
national listing feed can supply observations outside those categories.

## Data and derived information

Records include listing identifiers and titles; advertised prices and currency;
category, brand/model and condition; municipality; available publication/edit
timestamps and observation times; public availability/disposal labels, seller
type and image count; derived product labels, predictions and observation history.

Seller linkage is enabled. A public account identifier is converted to a stable
keyed HMAC before storage, and the raw identifier is discarded. The pseudonymous
key groups listings for seller concentration, archetype and relist analysis.
It is not anonymous data. Derived patterns may describe seller activity; the
assessment includes the effects of linkage and profiling.

The listing parser excludes seller names/contact details, precise coordinates,
postal/street addresses and raw account identifiers. Search/item response bodies
are not saved. Titles may nevertheless contain incidental personal information.
Private messages, seller profiles and login-gated pages are outside collection.

## Proposed basis, access and safeguards

The proposed basis is legitimate interests under Article 6(1)(f) GDPR: market
information for the controller's own purchasing and resale decisions. The
assessment and controller decision are documented in legal/LIA.md.

The workspace and inference service run locally. Access is intended for the
workspace operator; listing text is not sent to an external inference provider.
No additional recipients or international transfers are documented. The
controller must confirm this against the actual deployment before publication.

**Update, 2026-10-04 — this paragraph no longer describes the deployment.**
The repository now builds a hosted deployment in addition to the local one. The
local workspace and local inference service still exist, and inference is still
loopback-only; but the application itself is also served over the network by
`toriresearch/cloud_app.py` behind a Netlify gateway, against the same
database. Access is therefore not limited to the workspace operator: it is
granted to authenticated accounts holding a `viewer`, `seller` or `developer`
role in one organisation workspace, and every request must carry a gateway
signature and a valid session.

Because that changes who can reach the data, this draft must not be published in
its current form. Before it is, the controller must state in the notice itself:

- **Recipients.** The hosting provider is a recipient of personal data, and the
  notice must name it. Its processing terms and any sub-processors are not
  recorded anywhere in this repository and have to be obtained.
- **Transfers.** The deployment region is not recorded. If the provider or any
  sub-processor processes outside the EEA, Article 44 et seq. applies and the
  notice must say so and identify the safeguard relied on.
- **Retention.** The retention schedule in `legal/LIA.md` describes the
  application's own limits. The provider's retention of its own copies is not
  documented and must be established before any retention statement is made.
- **Data subjects.** Access, correction, erasure, restriction and objection are
  handled by the controller, but a subject who reaches the hosted application
  now has a second route to the data alongside the contact address. Both routes
  need to be stated, and neither is established as tested: see `legal/LIA.md` §5,
  which records that no automated suppression workflow or end-to-end
  request-handling test was found.

Until those are supplied, the honest position is that this notice is a draft and
the deployment is not ready to be published against it.

The collection flow supports human research decisions. It does not automatically
contact sellers, negotiate, buy or sell, or implement solely automated decisions
producing legal or similarly significant effects on sellers.

## Retention

| Data | Current configured retention |
|---|---|
| Retained feed/metadata response bodies | 14 days |
| Unlinked listing-level records and children | 400 days since last observation |
| Request audit records | 400 days |
| Managed automatic recovery snapshots | 7 days |

Listings linked to owned purchases/transactions are preserved with the private
ledger. Separate schedules for those records, manual backups and aggregate/model
artifacts need controller review. Aggregate coarsening alone does not establish
anonymisation or justify indefinite retention. Complete this section with
approved schedules before issuing the notice.

## Rights, objections and opt-out requests

You may request access, correction, erasure or restriction where applicable, and
object to processing based on legitimate interests. Portability depends on the
applicable basis and circumstances. Send requests to the contact above with
relevant listing URLs or identifiers and only the information needed to identify
the records.

The controller must establish a procedure to locate affected records, stop
applicable processing and prevent recollection where an objection is upheld.
No automated opt-out or end-to-end request-handling test is recorded; this draft
does not claim that either is operational.

Requests will be assessed under applicable GDPR rights and response periods.
You may complain to the [Office of the Data Protection Ombudsman in Finland](https://tietosuoja.fi/en/home).

Reference: [GDPR, Articles 12, 14, 15–21 and 77](https://eur-lex.europa.eu/eli/reg/2016/679/oj).
