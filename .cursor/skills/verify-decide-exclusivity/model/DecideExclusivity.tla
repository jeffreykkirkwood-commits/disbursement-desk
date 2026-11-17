---------------------------- MODULE DecideExclusivity ----------------------------
(***************************************************************************)
(* Model of disbursement-desk concurrent decide exclusivity (JSP-17).      *)
(*                                                                         *)
(* Promise: overlapping approve/clear decide() calls on one payout settle  *)
(* at most once when BEGIN IMMEDIATE serializes the save. Hold-then-approve*)
(* is allowed sequentially (#41); this model races settle actions only.    *)
(*                                                                         *)
(* Code map (payouts.decide):                                              *)
(*   status          == payouts.status (FLAGGED|HELD|PAID)                 *)
(*   settleCount     == successful Approve/Clear settles to PAID           *)
(*   DecideLocked    == body under BEGIN IMMEDIATE (Locked = TRUE)         *)
(*   See / Commit    == check-then-act without the transaction lock         *)
(*   usedKey[c]      == idempotency_key already stored for client c        *)
(*                                                                         *)
(* CONSTANTS Locked = TRUE  models current code (BEGIN IMMEDIATE).         *)
(*           Locked = FALSE models concurrent check-then-write.            *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Clients, MaxSteps, Locked
ASSUME MaxSteps \in Nat /\ MaxSteps >= 1
ASSUME Locked \in BOOLEAN
ASSUME Cardinality(Clients) >= 2

VARIABLES
  status,         \* "FLAGGED" | "PAID"
  settleCount,    \* Nat — committed Approve/Clear settles
  usedKey,        \* [client -> BOOLEAN]
  pc,             \* [client -> {"idle","seen","done"}]
  steps

vars == <<status, settleCount, usedKey, pc, steps>>

TypeOK ==
  /\ status \in {"FLAGGED", "PAID"}
  /\ settleCount \in 0..Cardinality(Clients)
  /\ DOMAIN usedKey = Clients
  /\ DOMAIN pc = Clients
  /\ \A c \in Clients: usedKey[c] \in BOOLEAN
  /\ \A c \in Clients: pc[c] \in {"idle", "seen", "done"}
  /\ steps \in 0..MaxSteps

Inv_AtMostOneSettle == settleCount <= 1

Decidable == status = "FLAGGED"

Init ==
  /\ status = "FLAGGED"
  /\ settleCount = 0
  /\ usedKey = [c \in Clients |-> FALSE]
  /\ pc = [c \in Clients |-> "idle"]
  /\ steps = 0

\* Atomic approve under BEGIN IMMEDIATE: replay same key, else settle once if decidable.
DecideLocked(c) ==
  /\ Locked
  /\ pc[c] = "idle"
  /\ steps < MaxSteps
  /\ steps' = steps + 1
  /\ pc' = [pc EXCEPT ![c] = "done"]
  /\ IF usedKey[c]
     THEN UNCHANGED <<status, settleCount, usedKey>>
     ELSE IF Decidable
          THEN /\ usedKey' = [usedKey EXCEPT ![c] = TRUE]
               /\ settleCount' = settleCount + 1
               /\ status' = "PAID"
          ELSE UNCHANGED <<status, settleCount, usedKey>>

See(c) ==
  /\ ~Locked
  /\ pc[c] = "idle"
  /\ steps < MaxSteps
  /\ Decidable
  /\ ~usedKey[c]
  /\ pc' = [pc EXCEPT ![c] = "seen"]
  /\ steps' = steps + 1
  /\ UNCHANGED <<status, settleCount, usedKey>>

Commit(c) ==
  /\ ~Locked
  /\ pc[c] = "seen"
  /\ steps < MaxSteps
  /\ steps' = steps + 1
  /\ pc' = [pc EXCEPT ![c] = "done"]
  /\ usedKey' = [usedKey EXCEPT ![c] = TRUE]
  /\ settleCount' = settleCount + 1
  /\ status' = "PAID"

ReplayUnlocked(c) ==
  /\ ~Locked
  /\ pc[c] = "idle"
  /\ usedKey[c]
  /\ steps < MaxSteps
  /\ pc' = [pc EXCEPT ![c] = "done"]
  /\ steps' = steps + 1
  /\ UNCHANGED <<status, settleCount, usedKey>>

Next ==
  \/ \E c \in Clients: DecideLocked(c)
  \/ \E c \in Clients: See(c)
  \/ \E c \in Clients: Commit(c)
  \/ \E c \in Clients: ReplayUnlocked(c)

Spec == Init /\ [][Next]_vars

====
