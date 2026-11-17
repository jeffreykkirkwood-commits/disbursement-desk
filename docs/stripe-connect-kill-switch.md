# Disarm Stripe payouts

`STRIPE_PAYOUTS_ENABLED` is the kill switch for new test-mode Transfers.
Disarming is an environment change and a process restart.

Names and accepted shapes for the Stripe variables are in the
[Stripe environment list in the README](../README.md#stripe-environment).
Leave secret values out of this file. `render.yaml` omits
`STRIPE_PAYOUTS_ENABLED`. Set it on the host with the variables in that
list.

## Stop new Transfers

The process reads `STRIPE_PAYOUTS_ENABLED` on each send. Only the exact
value `1` arms Transfers. `0`, `true`, `yes`, an empty value, and a
trailing space after the digit 1 all leave Transfers off.

Clearing `STRIPE_SECRET_KEY` while the flag remains `1` leaves the send
path armed and misconfigured. Approve then returns HTTP 409 with
`STRIPE_SEND_BLOCKED` and writes nothing.

1. Set `STRIPE_PAYOUTS_ENABLED` to `0`, or unset it.
2. Restart the process so it loads the new environment. A process that is already running keeps its old environment until it exits. Use one of these.
   - On Render, save the variable in the service settings. Render starts the service again from the current code.
   - In a local shell, stop the process and start `.venv/bin/python app.py` again.
3. Approve one payout in the pilot demo. Confirm `execution_ref` is `demo:` followed by the payout id. Confirm there is no new `tr_` id.

With the flag off, that approve uses the demo settle even when the other
Stripe secrets are still present. `status` is `PAID`, `execution_status`
is `SETTLED`, and the execution event source is `demo`. The process makes
no Transfer API call.

With the flag set to `1`, only the demo folder named by `STRIPE_DEMO_ID`
can create Transfers. Every other demo folder still uses the demo settle.
A missing or invalid Stripe setting in that armed state returns HTTP 409
with `STRIPE_SEND_BLOCKED`.

## Webhooks while the flag is off

`POST /stripe/webhook` ignores `STRIPE_PAYOUTS_ENABLED`.

These results hold when the demo file `payments.sqlite3` is still on disk:

- Stripe can still post to the endpoint.
- The handler checks `Stripe-Signature` against `STRIPE_WEBHOOK_SECRET`.
- A valid test-mode `transfer.created` event for a payout already in `SUBMITTED` sets `execution_status` to `SETTLED` and keeps the `tr_` id. Desk `status` stays `PAID`.
- A valid `transfer.reversed` event for a payout still in `SUBMITTED` sets `execution_status` to `FAILED`. Desk `status` stays `PAID`.
- A `transfer.reversed` event after `execution_status` is already `SETTLED` returns HTTP 200 and leaves `SETTLED` in place.
- A payout still in `NOT_STARTED` gets HTTP 503. The handler writes nothing.
- A live-mode event gets HTTP 422 with `STRIPE_SEND_BLOCKED`. The handler writes nothing.
- An unknown demo id gets HTTP 200. The handler writes nothing.

So a Transfer already in `SUBMITTED` can still settle after the flag is
off, for as long as that demo file remains.

On the free Render service, files under `DEMO_DIRECTORY` are lost on
deploy, restart, and spin-down. The restart in the disarm steps drops the
local row. The webhook then finds no demo file, returns HTTP 200, and
writes nothing. The Transfer can still exist at Stripe. See
[Deploy posture](deploy-posture.md). Keep `STRIPE_WEBHOOK_SECRET` set when
the demo file will survive the restart and the in-flight Transfer should
still settle.

## Stop webhook settlement

Use these steps when an in-flight Transfer must stay out of `SETTLED` and
`FAILED`, and the demo file is still on disk.

1. Remove `STRIPE_WEBHOOK_SECRET`, or replace it with a new `whsec_` value that the Stripe endpoint is not using.
2. Restart the process by the same Render save or local start as above.
3. Later posts fail the signature check and return HTTP 400. The handler writes nothing.

## Arm Transfers again

1. Set `STRIPE_PAYOUTS_ENABLED` to exactly `1`.
2. Check the other names in the [Stripe environment list in the README](../README.md#stripe-environment).
3. Restart the process.
4. Use a test key. The send path refuses a key that starts with `sk_live_` or `rk_live_`.

## Related

- [Stripe Connect go-live](stripe-connect-go-live.md)
- [Account ownership](stripe-connect-account-ownership.md)
- [Deploy posture](deploy-posture.md)
