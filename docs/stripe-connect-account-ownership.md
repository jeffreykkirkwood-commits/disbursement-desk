# Express accounts for the Connect pilot

This file recommends Express connected accounts for the Disbursement Desk
Connect pilot. The owner sign-off box at the end is unchecked. Jeff has not
signed the recommendation. The go-live checklist item stays open until
that box is checked.

The send path defaults to a test-mode destination charge
(`PaymentIntent` with `transfer_data[destination]`) to an `acct_` id.
`STRIPE_SEND_PATH=transfer` keeps the legacy platform-balance Transfer path.
Connected-account onboarding UI is still out of scope. See
[Stripe Connect go-live](stripe-connect-go-live.md) and
[Disarm Stripe payouts](stripe-connect-kill-switch.md).

## Recommendation

Use Express accounts for payees in this pilot.

Stripe hosts Express onboarding and collects the connected account's
required information. The Desk stores only the resulting `acct_` id, in
`STRIPE_DESTINATION_MAP` or on the payout column
`stripe_connected_account_id`. Custom accounts would move that collection
onto the platform. This service has no store for identity documents. The
go-live checklist leaves onboarding UI out of scope.

The go-live checklist asks for Express or Custom. Standard accounts
require OAuth onboarding. This send path stops at an `acct_` id, so
Standard accounts are outside this recommendation.

A later move to Custom means a new Stripe connected account and a new
`acct_` id in the destination map. Changing a Dashboard setting does not
turn an Express account into a Custom account.

## Tradeoffs

Express:

- Stripe hosts onboarding and verifies the connected account.
- The payee can open an Express Dashboard. That screen is Stripe's.
- Identity documents stay in Stripe's hosted flow. The Desk does not get a copy through this integration.
- The Transfer call matches the send path that exists today.
- Operators confirm Express country and business-type coverage in Stripe's docs before they choose payees.

Custom:

- The platform would build onboarding and collect the fields Stripe requires.
- The platform would record the connected account's acceptance of Stripe's terms and send that acceptance to Stripe.
- The platform would hold identity data before Stripe verifies it.
- The Transfer call could stay the same. The new cost is the work before an `acct_` id exists.
- That work needs the onboarding UI and a durable store. The Desk disk is not a durable store. See [Deploy posture](deploy-posture.md).

## KYC ownership

The platform Stripe account and the connected account are different
verification subjects.

Stripe verifies the platform legal entity when the platform account is
opened. That file stays in the Stripe platform account. Desk OFAC Clear
still runs in code before any Stripe money-movement call. That Clear
remains the sanctions gate. The go-live checklist already marks it met.

The connected account is the payee's legal entity. Under this
recommendation, Stripe collects and verifies that entity during
Stripe-hosted onboarding. Those documents stay in that hosted flow.

Under Custom, the platform would collect the payee's information and
submit it. Stripe would still make the verification decision. This
recommendation leaves collection with Stripe.

## Who signs

The payee's representative accepts Stripe's connected-account terms
during Stripe-hosted onboarding. A Desk operator signs those terms only
when that operator is the payee's representative.

Under Custom, the representative would still have to accept. The platform
would show the terms, record the acceptance, and send it to Stripe. This
recommendation leaves that acceptance in Stripe-hosted onboarding.

The owner of the platform Stripe account accepts Stripe's platform
agreement on that account. The platform account owner is unnamed here.

## Open questions

These points are still open:

- Which countries and entity types the pilot payees are, and whether Express is offered for each one.
- Whether any payee is the same legal entity as the platform. In that case there is no separate connected-account verification.
- Who owns the platform Stripe account, and whether that owner has already accepted Stripe's platform agreement.
- How negative balances on destination charges or Transfers are allocated. That choice belongs to the Stripe account settings, and it is still open.
- Whether payees should see Stripe branding and the Express Dashboard.
- Compliance review outside this repo. That go-live checklist item stays unchecked.
- Test-mode end-to-end on Connect test clocks, then a limited live pilot. That go-live checklist item stays unchecked.

## Owner sign-off

Jeff has not signed this recommendation.

- [ ] Jeff signs off on Express connected accounts for the Connect pilot.
- Date:
