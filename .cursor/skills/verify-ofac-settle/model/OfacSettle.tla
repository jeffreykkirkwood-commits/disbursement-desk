------------------------------ MODULE OfacSettle ------------------------------
(***************************************************************************)
(* Model of disbursement-desk OFAC settle gate (#111).                     *)
(*                                                                         *)
(* Promise: a payout whose payee still matches OFAC never reaches          *)
(* status=PAID / execution SETTLED unless the operator used Clear.         *)
(*                                                                         *)
(* Code map:                                                               *)
(*   Match              == ofac.screen(payee_name) is not None             *)
(*   flaggedSanctions   == POSSIBLE_SANCTIONS_MATCH in flag_code           *)
(*   cleared            == decide called _settle(..., allow_sanctions_clear=TRUE) *)
(*   status / settled   == payouts.status / execution_status==SETTLED      *)
(*   RouteOne           == payout_rules.route_one (explain_flag + settle)  *)
(*   DecideApprove      == payouts.decide(..., action=approve)             *)
(*   DecideClear        == payouts.decide(..., action=clear)               *)
(*   DecideBlock        == payouts.decide(..., action=block)               *)
(*   SettleScreen       == _settle re-screen when not allow_sanctions_clear*)
(*                                                                         *)
(* CONSTANTS GateCorrect = TRUE  models current gates.                     *)
(*           GateCorrect = FALSE models approve+flagged allowed and        *)
(*           settle skipping the OFAC re-screen (broken design).           *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS MaxSteps, GateCorrect, Match
ASSUME MaxSteps \in Nat /\ MaxSteps >= 1
ASSUME GateCorrect \in BOOLEAN
ASSUME Match \in BOOLEAN

VARIABLES
  status,            \* "PENDING" | "FLAGGED" | "HELD" | "RETURNED" | "PAID"
  flaggedSanctions,  \* BOOLEAN
  cleared,           \* BOOLEAN — Clear was the settle path
  settled,           \* BOOLEAN — execution SETTLED / status PAID written
  steps

vars == <<status, flaggedSanctions, cleared, settled, steps>>

TypeOK ==
  /\ status \in {"PENDING", "FLAGGED", "HELD", "RETURNED", "PAID"}
  /\ flaggedSanctions \in BOOLEAN
  /\ cleared \in BOOLEAN
  /\ settled \in BOOLEAN
  /\ steps \in 0..MaxSteps

\* Core promise: PAID/SETTLED only if no OFAC match, or Clear allowed the send.
Inv_NoSettleWithoutClear ==
  settled => (~Match \/ cleared)

Init ==
  /\ status = "PENDING"
  /\ flaggedSanctions = FALSE
  /\ cleared = FALSE
  /\ settled = FALSE
  /\ steps = 0

\* payout_rules.route_one: explain_flag includes ofac.screen; match => FLAGGED only.
\* Clean path writes PAID+SETTLED then _settle (already=True). GateCorrect=FALSE
\* models a broken route that settles despite Match (skipped OFAC in explain_flag).
RouteOne ==
  /\ status = "PENDING"
  /\ steps < MaxSteps
  /\ steps' = steps + 1
  /\ UNCHANGED cleared
  /\ IF Match /\ GateCorrect
     THEN /\ status' = "FLAGGED"
          /\ flaggedSanctions' = TRUE
          /\ settled' = FALSE
     ELSE IF Match /\ ~GateCorrect
     THEN /\ status' = "PAID"
          /\ flaggedSanctions' = FALSE
          /\ settled' = TRUE
     ELSE /\ status' = "PAID"
          /\ flaggedSanctions' = FALSE
          /\ settled' = TRUE

\* Operator hold from FLAGGED (non-sanctions path still allowed).
DecideHold ==
  /\ status \in {"FLAGGED", "HELD"}
  /\ steps < MaxSteps
  /\ status' = "HELD"
  /\ steps' = steps + 1
  /\ UNCHANGED <<flaggedSanctions, cleared, settled>>

\* return without sanctions flag (model allows from FLAGGED/HELD).
DecideReturn ==
  /\ status \in {"FLAGGED", "HELD"}
  /\ steps < MaxSteps
  /\ status' = "RETURNED"
  /\ steps' = steps + 1
  /\ UNCHANGED <<flaggedSanctions, cleared, settled>>

\* block only when sanctions flagged (else 409 in code — omitted as stutter).
DecideBlock ==
  /\ status \in {"FLAGGED", "HELD"}
  /\ flaggedSanctions
  /\ steps < MaxSteps
  /\ status' = "RETURNED"
  /\ steps' = steps + 1
  /\ UNCHANGED <<flaggedSanctions, cleared, settled>>

\* approve: refuse when flaggedSanctions (GateCorrect); else pre-screen hold on Match;
\* else settle. Broken gate lets approve settle a Match without Clear.
DecideApprove ==
  /\ status \in {"FLAGGED", "HELD"}
  /\ steps < MaxSteps
  /\ steps' = steps + 1
  /\ IF flaggedSanctions /\ GateCorrect
     THEN UNCHANGED <<status, flaggedSanctions, cleared, settled>>  \* 409
     ELSE IF Match /\ GateCorrect
     THEN /\ status' = "FLAGGED"           \* pre-screen or _settle screen hold
          /\ flaggedSanctions' = TRUE
          /\ UNCHANGED <<cleared, settled>>
     ELSE IF Match /\ ~GateCorrect
     THEN /\ status' = "PAID"              \* broken: settle match without Clear
          /\ settled' = TRUE
          /\ UNCHANGED <<flaggedSanctions, cleared>>
     ELSE /\ status' = "PAID"
          /\ settled' = TRUE
          /\ UNCHANGED <<flaggedSanctions, cleared>>

\* clear: only when flaggedSanctions; settles even if Match (allow_sanctions_clear).
DecideClear ==
  /\ status \in {"FLAGGED", "HELD"}
  /\ flaggedSanctions
  /\ steps < MaxSteps
  /\ status' = "PAID"
  /\ settled' = TRUE
  /\ cleared' = TRUE
  /\ flaggedSanctions' = flaggedSanctions
  /\ steps' = steps + 1

\* Seed-style: already at FLAGGED with sanctions (demo OFAC payee after route).
\* Models starting after automation flagged a match (not PENDING).
StartFlaggedMatch ==
  /\ steps = 0
  /\ status = "PENDING"
  /\ Match
  /\ status' = "FLAGGED"
  /\ flaggedSanctions' = TRUE
  /\ cleared' = FALSE
  /\ settled' = FALSE
  /\ steps' = 1

Next ==
  \/ RouteOne
  \/ DecideHold
  \/ DecideReturn
  \/ DecideBlock
  \/ DecideApprove
  \/ DecideClear
  \/ StartFlaggedMatch

Spec == Init /\ [][Next]_vars

====
