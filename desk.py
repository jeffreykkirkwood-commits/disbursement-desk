# START HERE — Builds the Disbursement Desk page (HTML): the read-only payout queue.
# Read-only: it never changes the database. Slice 1 of issue #32 (no detail panel,
# no actions, no notes yet; those come in later slices).
"""Disbursement Desk presentation for the approved #40 design. Never writes to the database."""
from dashboard import money, text

# FEATURE 01 — Turn status codes into the approved plain words
# The API keeps its own codes; viewers only ever see these four words (#40). PENDING
# and APPROVED are not in the seed; if one appears it is shown as the nearest word,
# never as the raw code.
STATUS_WORDS = {
    'FLAGGED': ('Needs review', 's-review'),
    'PENDING': ('Needs review', 's-review'),
    'HELD': ('Held', 's-held'),
    'RETURNED': ('Returned', 's-returned'),
    'PAID': ('Sent', 's-sent'),
    'APPROVED': ('Sent', 's-sent'),
}
EXCEPTION_STATUSES = frozenset({'FLAGGED', 'PENDING', 'HELD'})
LOAD_ERROR = 'The payout list couldn’t be loaded. Refresh the page to try again.'
EMPTY_EXCEPTIONS = 'No exceptions left. Nice.'


# FEATURE 02 — Build one queue row
# Payee, amount like $4,850.00, the payout id plus why it was flagged (or the rail when
# nothing fired) and the status pill. Rows that are not exceptions start hidden because
# the page opens on Exceptions.
def _row(payout):
    word, css = STATUS_WORDS[payout['status']]
    exception = payout['status'] in EXCEPTION_STATUSES
    why = payout['flag_text'] or payout['rail']
    return (f'<li data-ex="{int(exception)}"{"" if exception else " hidden"}><div class="row">'
            f'<span class="payee">{text(payout["payee_name"])}</span>'
            f'<span class="amt">{money(payout["amount"])}</span>'
            f'<span class="sub">{text(payout["payout_id"])} · {text(why)}</span>'
            f'<span class="pill {css}">{word}</span></div></li>')


# FEATURE 03 — Build the whole Desk page
# queue is the current-queue payouts (payouts.current_queue), or None when it could not be
# read, which shows the approved load-error sentence instead of the list (Q2 option A).
# The list is part of the first paint, so there is no loading message.
def render_desk(queue, demo_id='local-test'):
    if queue is None:
        exceptions = total = ''
        items = f'<li class="err" role="alert">{LOAD_ERROR}</li>'
    else:
        ex_count = sum(p['status'] in EXCEPTION_STATUSES for p in queue)
        exceptions, total = f' <b>{ex_count}</b>', f' <b>{len(queue)}</b>'
        items = ''.join(_row(p) for p in queue)
        items += f'<li class="empty" data-empty{"" if ex_count == 0 else " hidden"}>{EMPTY_EXCEPTIONS}</li>'
    return '''<!doctype html>
<html lang="en"><head><meta name="demo-id" content="''' + text(demo_id) + '''"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Disbursement Desk · Example data</title>
<style>
:root{color-scheme:light;--ink:#173b35;--muted:#4f6159;--line:#d5ddd5;--paper:#f5f6f0;--green:#176348;--amber-bg:#fff2d6;--amber:#6b4d0f;--red-bg:#f9ebe5;--red:#8a3523;--ok-bg:#e8f2eb;--ok:#1f5a3f;--blue-bg:#e7eefb;--blue:#234a8a}
*{box-sizing:border-box}html,body{margin:0;overflow-x:hidden}
body{background:var(--paper);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
button{font:inherit;cursor:pointer}
:focus-visible{outline:3px solid #b38528;outline-offset:2px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 24px;background:#fff;border-bottom:1px solid var(--line)}
.top .right{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.brand{font-weight:750;font-size:19px;display:flex;gap:10px;align-items:center}
.mark{background:var(--ink);color:#fff;border-radius:8px;width:32px;height:32px;display:grid;place-items:center;flex-shrink:0}
.tag{font-size:13px;font-weight:700;background:var(--amber-bg);color:var(--amber);padding:4px 10px;border-radius:6px;white-space:nowrap}
.lab-link{font-size:14px;font-weight:600;color:var(--green);text-decoration:underline;margin-right:12px;white-space:nowrap;min-height:44px;display:inline-flex;align-items:center}
.layout{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;max-width:1240px;margin:auto;padding:24px}
h1{font-size:26px;margin:0;letter-spacing:-.5px}
.qhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.filters{display:flex;gap:8px}
.chip{min-height:44px;min-width:44px;padding:0 16px;border-radius:22px;border:1px solid var(--line);background:#fff;color:var(--ink);font-weight:600}
.chip[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.chip b{margin-left:4px}
.list{list-style:none;margin:0;padding:0;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.list li[hidden]{display:none}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 16px;width:100%;min-height:64px;padding:14px 18px;background:#fff;color:var(--ink);box-shadow:0 -1px 0 var(--line)}
/* The line above each row; .list clips it above the first visible row. */
.payee{font-weight:650;overflow-wrap:anywhere}.amt{font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.sub{font-size:14px;color:var(--muted)}
.pill{display:inline-block;font-size:13px;font-weight:700;padding:2px 9px;border-radius:5px;justify-self:end;align-self:start}
.s-review{background:var(--amber-bg);color:var(--amber)}.s-held{background:var(--blue-bg);color:var(--blue)}
.s-returned{background:var(--red-bg);color:var(--red)}.s-sent{background:var(--ok-bg);color:var(--ok)}
.empty{padding:18px;color:var(--muted);box-shadow:0 -1px 0 var(--line)}
.err{background:var(--red-bg);color:var(--red);border-radius:8px;padding:14px 16px;margin:8px;font-size:15px}
.foot{font-size:14px;color:var(--muted);margin-top:12px}
.link{background:none;border:0;color:var(--green);text-decoration:underline;font-weight:600;min-height:44px;padding:0 8px}
@media(max-width:760px){
 .top{padding:12px 16px}.brand{font-size:17px}.tag{font-size:12px}
 .layout{padding:16px}
}
</style></head>
<body>
<header class="top">
  <div class="brand"><span class="mark" aria-hidden="true">D</span>Disbursement Desk</div>
  <div class="right"><a class="lab-link" href="/">Payments Lab</a><span class="tag">Example data · fake money</span></div>
</header>
<main class="layout">
  <section class="queue" aria-labelledby="qh">
    <div class="qhead">
      <h1 id="qh">Outgoing payouts</h1>
      <div class="filters" role="group" aria-label="Filter">
        <button type="button" class="chip" data-f="exceptions" aria-pressed="true">Exceptions''' + exceptions + '''</button>
        <button type="button" class="chip" data-f="all" aria-pressed="false">All''' + total + '''</button>
      </div>
    </div>
    <ul id="list" class="list">''' + items + '''</ul>
    <p class="foot">Your changes stay in this browser only. <button type="button" id="reset" class="link">Reset demo</button></p>
  </section>
</main>
<script src="/desk.js"></script>
</body></html>'''
