# START HERE — Builds the Disbursement Desk page (HTML): the payout queue, the payout
# detail, the Approve / Hold / Return confirm dialog and the note badges (issue #32
# slices 1 to 5). This file never changes the database: desk.js sends a confirmed
# decision to the #35 decision API (POST /payouts/<id>/<action>) and reloads. Slice 5
# reads each payout's stored #37 note (note_text, note_source, note_fallback) and
# shows it on screen. It does not call a model or invent note sentences.
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
FINAL_STATUSES = frozenset({'PAID', 'RETURNED'})
FINAL_MESSAGE = 'This payout is final. No actions available.'
ACTION_FAILED = 'That didn’t save. Nothing has changed. Try again.'
RESET_FAILED = 'That didn’t reset. Nothing has changed. Try again.'

# FEATURE 01b — The three confirm dialogs, word for word from #40
# action: (button, heading, body, confirm-button classes). Return's confirm is red.
ACTIONS = {
    'approve': ('Approve', 'Approve and send?', 'The payout is sent to the payee.', 'btn primary'),
    'hold': ('Hold', 'Hold this payout?',
             'It stays in Exceptions and will not send until someone approves it.', 'btn primary'),
    'return': ('Return', 'Return this payout?',
               'It is cancelled and no money moves. This cannot be undone.', 'btn danger fill'),
}
PANEL_BUTTON_CLASS = {'approve': 'btn primary', 'hold': 'btn', 'return': 'btn danger'}
OPERATOR_FLAGS = {'HELD': 'Held by operator', 'RETURNED': 'Returned by operator'}


# FEATURE 01c — Which flag sentence to show
# A payout an operator has decided (its updated_at moved past created_at; the seed keeps
# them equal) shows Held by operator / Returned by operator, and an approved (Sent) one
# shows no flag. Undecided payouts keep their stored rule sentence. Read-only.
def shown_flag(payout):
    decided = payout.get('updated_at') and payout['updated_at'] != payout.get('created_at')
    if not decided:
        return payout['flag_text']
    if payout['status'] in ('PAID', 'APPROVED'):
        return None
    return OPERATOR_FLAGS.get(payout['status'], payout['flag_text'])


# FEATURE 01d — Choose the note badge and helper line (slice 5, Q3 option A)
# Stored #37 notes come from a template, so they are shown only as RULES NOTE
# plus "From fixed rules". There is no AI model, so the AI NOTE spot uses the
# approved #40 fallback wording: RULES NOTE plus "AI unavailable, rules fallback".
# The body is the payout's stored note_text. "Written by AI, may be wrong" and
# "AI-drafted, review required" are never shown on template text. This slice does
# not call a model, add an env var or key, or write note columns. The AI never
# decides the flag.
RULES_NOTE_BADGE = 'RULES NOTE'
RULES_NOTE_HELPER = 'From fixed rules'
FALLBACK_HELPER = 'AI unavailable, rules fallback'
AI_TIMEOUT_SECONDS = 3


def ai_unavailable(ai_off=False, error=False, elapsed_seconds=None):
    if ai_off or error:
        return True
    return elapsed_seconds is not None and elapsed_seconds > AI_TIMEOUT_SECONDS


def shown_note(payout, ai_off=False, error=False, elapsed_seconds=None):
    """Badge, helper line, body, kind and whether this is the fallback."""
    body = payout.get('note_text') or ''
    # No model: never label stored text as Written by AI, even if note_source is ai.
    if (payout.get('note_fallback') or payout.get('note_source') == 'ai'
            or ai_unavailable(ai_off, error, elapsed_seconds)):
        return {
            'badge': RULES_NOTE_BADGE,
            'helper': FALLBACK_HELPER,
            'body': body,
            'kind': 'rules',
            'fallback': True,
        }
    return {
        'badge': RULES_NOTE_BADGE,
        'helper': RULES_NOTE_HELPER,
        'body': body,
        'kind': 'rules',
        'fallback': False,
    }


def _note_block(note, hidden=False):
    css = 'b-ai' if note['kind'] == 'ai' else 'b-rules'
    if note['fallback']:
        which = 'fallback'
    elif note['kind'] == 'ai':
        which = 'ai'
    else:
        which = 'rules'
    hide = ' hidden' if hidden else ''
    return (f'<div class="note" data-note="{which}"{hide}><div class="note-h">'
            f'<span class="badge {css}">{note["badge"]}</span>{text(note["helper"])}</div>'
            f'<p>{text(note["body"])}</p></div>')


def _notes(payout):
    # The stored rules note is visible on first paint, so the panel never sits
    # empty or keeps spinning. The approved AI-unavailable fallback (same stored
    # body, fallback helper) is also in the page, hidden, and desk.js shows it
    # when #ai=off. No model is called.
    default = shown_note(payout)
    blocks = [_note_block(default)]
    if not default['fallback']:
        blocks.append(_note_block(shown_note(payout, ai_off=True), hidden=True))
    return ''.join(blocks)


# FEATURE 02 — Build one queue row
# Payee, amount like $4,850.00, the payout id plus why it was flagged (or the rail when
# nothing fired) and the status pill. Each row is a button that opens its detail (slice 2).
# Rows that are not exceptions start hidden because the page opens on Exceptions.
def _row(payout):
    word, css = STATUS_WORDS[payout['status']]
    exception = payout['status'] in EXCEPTION_STATUSES
    why = shown_flag(payout) or payout['rail']
    return (f'<li data-ex="{int(exception)}"{"" if exception else " hidden"}>'
            f'<button type="button" class="row" data-id="{text(payout["payout_id"])}" aria-current="false">'
            f'<span class="payee">{text(payout["payee_name"])}</span>'
            f'<span class="amt">{money(payout["amount"])}</span>'
            f'<span class="sub">{text(payout["payout_id"])} · {text(why)}</span>'
            f'<span class="pill {css}">{word}</span></button></li>')


# FEATURE 02b — Build one payout's detail, its note and its action buttons
# Payee, amount, payout id, account, rail, status, the flag sentence when one exists,
# and one note (slice 5) from the stored #37 columns. Sent and Returned payouts say
# they are final. Needs review and Held payouts get Approve / Hold / Return (slice 3);
# Hold is disabled when the payout is already Held. The panel carries the payee and
# amount so the dialog can repeat them.
def _detail(payout):
    word, css = STATUS_WORDS[payout['status']]
    pid = text(payout['payout_id'])
    flag = shown_flag(payout)
    parts = [f'<div class="panel" data-id="{pid}" data-payee="{text(payout["payee_name"])}" '
             f'data-amount="{money(payout["amount"])}" hidden>',
             '<div class="dtop"><button type="button" class="back" aria-label="Back to list">← Back</button>'
             f'<span class="pill {css}">{word}</span></div>',
             f'<h2 tabindex="-1">{text(payout["payee_name"])}</h2>',
             f'<p class="big">{money(payout["amount"])}</p>',
             f'<dl><dt>Payout</dt><dd>{pid}</dd><dt>To account</dt><dd>{text(payout["payee_account"])}</dd>'
             f'<dt>Rail</dt><dd>{text(payout["rail"])} · {text(payout["currency"])}</dd></dl>']
    if flag:
        parts.append(f'<div class="flag"><strong>Why flagged:</strong> {text(flag)}</div>')
    parts.append(_notes(payout))
    if payout['status'] in FINAL_STATUSES:
        parts.append(f'<p class="done">{FINAL_MESSAGE}</p>')
    else:
        buttons = []
        for action, (label, heading, body, ok_class) in ACTIONS.items():
            disabled = ' disabled' if action == 'hold' and payout['status'] == 'HELD' else ''
            buttons.append(f'<button type="button" class="{PANEL_BUTTON_CLASS[action]}" data-act="{action}" '
                           f'data-heading="{text(heading)}" data-body="{text(body)}" data-ok="{label}" '
                           f'data-ok-class="{ok_class}"{disabled}>{label}</button>')
        parts.append('<div class="actions">' + ''.join(buttons) + '</div>')
    parts.append('</div>')
    return ''.join(parts)


# FEATURE 03 — Build the whole Desk page
# queue is the current-queue payouts (payouts.current_queue), or None when it could not be
# read, which shows the approved load-error sentence instead of the list (Q2 option A).
# The list is part of the first paint, so there is no loading message.
def render_desk(queue, demo_id='local-test'):
    if queue is None:
        exceptions = total = details = ''
        items = f'<li class="err" role="alert">{LOAD_ERROR}</li>'
    else:
        ex_count = sum(p['status'] in EXCEPTION_STATUSES for p in queue)
        exceptions, total = f' <b>{ex_count}</b>', f' <b>{len(queue)}</b>'
        items = ''.join(_row(p) for p in queue)
        items += f'<li class="empty" data-empty{"" if ex_count == 0 else " hidden"}>{EMPTY_EXCEPTIONS}</li>'
        details = ''.join(_detail(p) for p in queue)
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
.layout{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:24px;max-width:1240px;margin:auto;padding:24px}
.layout:has(.detail[hidden]){grid-template-columns:minmax(0,1fr)}
h2{font-size:22px;margin:12px 0 0}h2:focus{outline:none}
h1{font-size:26px;margin:0;letter-spacing:-.5px}
.qhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.filters{display:flex;gap:8px}
.chip{min-height:44px;min-width:44px;padding:0 16px;border-radius:22px;border:1px solid var(--line);background:#fff;color:var(--ink);font-weight:600}
.chip[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.chip b{margin-left:4px}
.list{list-style:none;margin:0;padding:0;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.list li[hidden]{display:none}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 16px;width:100%;min-height:64px;padding:14px 18px;border:0;background:#fff;color:var(--ink);text-align:left;box-shadow:0 -1px 0 var(--line)}
.row[aria-current=true]{background:#eef3ea;box-shadow:0 -1px 0 var(--line),inset 4px 0 0 var(--green)}
/* Draw the keyboard focus ring inside the row so .list overflow:hidden does not clip it (#102). */
.row:focus-visible{outline-offset:-3px}
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
.detail{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px;align-self:start;position:sticky;top:16px}
.panel[hidden]{display:none}
.dtop{display:flex;justify-content:space-between;align-items:center;gap:8px}
.back{min-height:44px;min-width:44px;border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;padding:0 12px;font-weight:600}
.big{font-size:36px;font-weight:700;letter-spacing:-1px;margin:10px 0 0;font-variant-numeric:tabular-nums}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:14px 0;font-size:15px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.flag{background:var(--amber-bg);color:var(--amber);border-radius:8px;padding:10px 12px;font-size:15px;margin:12px 0}
.note{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}
.note[hidden]{display:none}
.note-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-weight:700;font-size:14px;margin-bottom:4px}
.badge{font-size:12px;font-weight:800;padding:2px 8px;border-radius:5px;letter-spacing:.04em}
.b-ai{background:#ece8fb;color:#4a2f99}.b-rules{background:#e9ece9;color:#2f3d37}
.note p{margin:4px 0;font-size:15px}
.done{font-size:15px;color:var(--muted);margin-top:14px}
.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}
.btn{min-height:48px;min-width:44px;border-radius:9px;font-weight:700;border:2px solid var(--ink);background:#fff;color:var(--ink);padding:0 12px}
.btn.primary{background:var(--green);border-color:var(--green);color:#fff}
.btn.danger{border-color:var(--red);color:var(--red)}
.btn.danger.fill{background:var(--red);color:#fff}
.btn:disabled{opacity:.45;cursor:not-allowed}
dialog{border:0;border-radius:14px;padding:22px;max-width:420px;width:calc(100% - 32px);color:var(--ink)}
dialog::backdrop{background:rgba(23,59,53,.45)}
dialog p{margin:8px 0}dialog h2{margin:0}
dialog .row2{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
dialog .btn{min-width:110px}
.err-inline{background:var(--red-bg);color:var(--red);border-radius:8px;padding:10px 12px;margin:12px 0 0;font-size:15px}
.err-inline[hidden]{display:none}
.foot .err-inline{display:inline;margin:0 0 0 .4em;padding:4px 8px;font-size:14px}
.foot .err-inline[hidden]{display:none}
@media(max-width:760px){
 .top{padding:12px 16px}.brand{font-size:17px}.tag{font-size:12px}
 .layout{grid-template-columns:minmax(0,1fr);padding:16px}
 .detail{position:static;padding:16px}
 .actions{grid-template-columns:1fr}
 .layout:has(.detail:not([hidden])) .queue{display:none}
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
    <p class="foot">Your changes stay in this browser only. <button type="button" id="reset" class="link">Reset demo</button><span id="r-error" class="err-inline" role="status" hidden>''' + RESET_FAILED + '''</span></p>
  </section>
  <aside id="detail" class="detail" aria-label="Payout detail" hidden>''' + details + '''</aside>
</main>
<dialog id="confirm" aria-labelledby="ch">
  <h2 id="ch"></h2>
  <p><strong id="c-payee"></strong> · <span id="c-amount"></span> · <span id="c-id"></span></p>
  <p id="c-body"></p>
  <p id="c-error" class="err-inline" role="alert" hidden>''' + ACTION_FAILED + '''</p>
  <div class="row2"><button type="button" class="btn" id="cancel">Cancel</button><button type="button" id="ok"></button></div>
</dialog>
<script src="/desk.js"></script>
</body></html>'''
