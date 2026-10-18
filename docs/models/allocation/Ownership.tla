---------------------------- MODULE Ownership ----------------------------
EXTENDS Naturals, FiniteSets
CONSTANT UnsafeJobNumberOnly
VARIABLES phase, generation, owner, actualEpoch, proof, cancelIntent,
          granted, unsafeEffect, staleSeen
vars == <<phase, generation, owner, actualEpoch, proof, cancelIntent,
          granted, unsafeEffect, staleSeen>>

Init == /\ phase = "pending" /\ generation = 1 /\ owner = "unseen"
        /\ actualEpoch = 0 /\ proof = 0 /\ cancelIntent = FALSE
        /\ granted = {} /\ unsafeEffect = FALSE /\ staleSeen = FALSE

Grant == /\ owner = "unseen" /\ generation \notin granted
         /\ owner' = "mine" /\ actualEpoch' = generation
         /\ granted' = granted \cup {generation}
         /\ UNCHANGED <<phase, generation, proof, cancelIntent, unsafeEffect, staleSeen>>

Observe == /\ phase \in {"pending", "active", "uncertain"}
           /\ proof' = IF owner = "mine" THEN actualEpoch ELSE 0
           /\ phase' = IF owner = "terminal" THEN "light"
                       ELSE IF owner = "mine" /\ ~cancelIntent THEN "active"
                       ELSE "uncertain"
           /\ UNCHANGED <<generation, owner, actualEpoch, cancelIntent, granted, unsafeEffect, staleSeen>>

RequestCancel == /\ ~cancelIntent /\ cancelIntent' = TRUE /\ phase' = "uncertain"
                 /\ UNCHANGED <<generation, owner, actualEpoch, proof, granted, unsafeEffect, staleSeen>>

CancelOwned == /\ cancelIntent /\ proof > 0
               /\ (UnsafeJobNumberOnly \/ (owner = "mine" /\ actualEpoch = proof /\ proof = generation))
               /\ unsafeEffect' = (unsafeEffect \/ owner # "mine" \/ actualEpoch # generation)
               /\ owner' = "terminal" /\ actualEpoch' = 0 /\ proof' = 0 /\ phase' = "uncertain"
               /\ UNCHANGED <<generation, cancelIntent, granted, staleSeen>>

RecycleSameNumber == /\ owner = "mine" /\ owner' = "other" /\ actualEpoch' = 3 - generation
                     /\ UNCHANGED <<phase, generation, proof, cancelIntent, granted, unsafeEffect, staleSeen>>

LoseChannel == /\ phase \in {"pending", "active"} /\ phase' = "uncertain"
               /\ UNCHANGED <<generation, owner, actualEpoch, proof, cancelIntent, granted, unsafeEffect, staleSeen>>

NewGeneration == /\ phase = "light" /\ owner = "terminal" /\ generation = 1
                 /\ generation' = 2 /\ phase' = "pending" /\ owner' = "unseen"
                 /\ proof' = 0 /\ cancelIntent' = FALSE
                 /\ UNCHANGED <<actualEpoch, granted, unsafeEffect, staleSeen>>

StalePublication == /\ staleSeen' = ~staleSeen
                    /\ UNCHANGED <<phase, generation, owner, actualEpoch, proof, cancelIntent, granted, unsafeEffect>>

Next == Grant \/ Observe \/ RequestCancel \/ CancelOwned \/ RecycleSameNumber \/ LoseChannel \/ NewGeneration \/ StalePublication
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"pending", "active", "uncertain", "light"}
          /\ generation \in {1, 2} /\ owner \in {"unseen", "mine", "other", "terminal"}
          /\ actualEpoch \in {0, 1, 2} /\ proof \in {0, 1, 2}
          /\ cancelIntent \in BOOLEAN /\ granted \subseteq {1, 2}
          /\ unsafeEffect \in BOOLEAN /\ staleSeen \in BOOLEAN
Safety == ~unsafeEffect
=============================================================================
