------------------------------ MODULE StreamSlots ------------------------------
(***************************************************************************)
(* Model of disbursement-desk payout_events stream slots (#83).            *)
(*                                                                         *)
(* Code map (payout_events.py):                                            *)
(*   openCount     == _stream_count                                        *)
(*   MaxSlots      == MAX_STREAMS                                          *)
(*   AcquireLocked == acquire_stream() under `with _stream_lock`           *)
(*   ReleaseLocked == release_stream() under `with _stream_lock`           *)
(*   AcquireSee / AcquireBump == check-then-increment without the lock     *)
(*   held[c]       == client c successfully acquired and not yet released  *)
(*                                                                         *)
(* CONSTANTS Locked = TRUE  models current code (lock held).               *)
(*           Locked = FALSE models concurrent check-then-bump.             *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Clients, MaxSlots, MaxSteps, Locked
ASSUME MaxSlots \in Nat /\ MaxSlots >= 1
ASSUME MaxSteps \in Nat /\ MaxSteps >= 1
ASSUME Locked \in BOOLEAN
ASSUME Cardinality(Clients) > MaxSlots

VARIABLES
  openCount,   \* Nat; mirrors _stream_count
  held,        \* [client -> BOOLEAN]
  pc,          \* [client -> {"idle","seen","holding"}]
  steps

vars == <<openCount, held, pc, steps>>

TypeOK ==
  /\ openCount \in 0..Cardinality(Clients)
  /\ DOMAIN held = Clients
  /\ DOMAIN pc = Clients
  /\ \A c \in Clients: held[c] \in BOOLEAN
  /\ \A c \in Clients: pc[c] \in {"idle", "seen", "holding"}
  /\ steps \in 0..MaxSteps

Inv_SlotsNeverExceeded == openCount <= MaxSlots

\* held clients and openCount stay aligned when Locked (atomic acquire/release).
\* Unlocked path may diverge mid-race; CapOK is the safety property we check.

Init ==
  /\ openCount = 0
  /\ held = [c \in Clients |-> FALSE]
  /\ pc = [c \in Clients |-> "idle"]
  /\ steps = 0

AcquireLocked(c) ==
  /\ Locked
  /\ pc[c] = "idle"
  /\ steps < MaxSteps
  /\ IF openCount < MaxSlots
     THEN /\ openCount' = openCount + 1
          /\ held' = [held EXCEPT ![c] = TRUE]
          /\ pc' = [pc EXCEPT ![c] = "holding"]
     ELSE /\ UNCHANGED <<openCount, held>>
          /\ pc' = [pc EXCEPT ![c] = "idle"]
  /\ steps' = steps + 1

ReleaseLocked(c) ==
  /\ Locked
  /\ pc[c] = "holding"
  /\ held[c]
  /\ steps < MaxSteps
  /\ openCount' = IF openCount > 0 THEN openCount - 1 ELSE 0
  /\ held' = [held EXCEPT ![c] = FALSE]
  /\ pc' = [pc EXCEPT ![c] = "idle"]
  /\ steps' = steps + 1

\* Unlocked path: observe under-cap, then later bump with no re-check.
AcquireSee(c) ==
  /\ ~Locked
  /\ pc[c] = "idle"
  /\ steps < MaxSteps
  /\ openCount < MaxSlots
  /\ pc' = [pc EXCEPT ![c] = "seen"]
  /\ steps' = steps + 1
  /\ UNCHANGED <<openCount, held>>

AcquireBump(c) ==
  /\ ~Locked
  /\ pc[c] = "seen"
  /\ steps < MaxSteps
  /\ openCount' = openCount + 1
  /\ held' = [held EXCEPT ![c] = TRUE]
  /\ pc' = [pc EXCEPT ![c] = "holding"]
  /\ steps' = steps + 1

ReleaseUnlocked(c) ==
  /\ ~Locked
  /\ pc[c] = "holding"
  /\ held[c]
  /\ steps < MaxSteps
  /\ openCount' = IF openCount > 0 THEN openCount - 1 ELSE 0
  /\ held' = [held EXCEPT ![c] = FALSE]
  /\ pc' = [pc EXCEPT ![c] = "idle"]
  /\ steps' = steps + 1

Next ==
  \/ \E c \in Clients: AcquireLocked(c)
  \/ \E c \in Clients: ReleaseLocked(c)
  \/ \E c \in Clients: AcquireSee(c)
  \/ \E c \in Clients: AcquireBump(c)
  \/ \E c \in Clients: ReleaseUnlocked(c)

Spec == Init /\ [][Next]_vars

====
