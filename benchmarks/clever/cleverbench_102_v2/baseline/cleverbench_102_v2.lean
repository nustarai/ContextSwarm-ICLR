import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_102_v2

@[reducible, simp]
def implementation_precond (n : Nat) (m : Nat) : Prop :=
  -- !benchmark @start precond
  True
  -- !benchmark @end precond

def implementation (n: Nat) (m: Nat) (h_precond : implementation_precond (n) (m)) : Option String :=
  -- !benchmark @start code
  if n > m then
    none
  else
    let xs := List.Ico n (m+1);
    -- The current upstream implementation and tests use Nat.div: this is floor average,
    -- not nearest-integer rounding.
    let avg := xs.sum / xs.length;
    some ("0b" ++ (Nat.toDigits 2 avg).asString)
  -- !benchmark @end code

@[reducible, simp]
def implementation_postcond (n : Nat) (m : Nat) (result : Option String) (h_precond : implementation_precond (n) (m)) : Prop :=
  -- !benchmark @start postcond
  (n > m ↔ result.isNone) ∧
  (n ≤ m ↔ result.isSome) ∧
  (n ≤ m →
    (result.isSome ∧
    let val := Option.getD result "";
    let xs := List.Ico n (m+1);
    let avg := xs.sum / xs.length;
    (val.take 2 = "0b") ∧
    (Nat.ofDigits 2 ((val.drop 2).toList.map (fun c => c.toNat - '0'.toNat)).reverse = avg)))
  -- !benchmark @end postcond

theorem implementation_postcond_satisfied (n : Nat) (m : Nat) (h_precond : implementation_precond (n) (m)) :
    implementation_postcond (n) (m) (implementation n m h_precond) h_precond := by
  -- !benchmark @start proof
  sorry
  -- !benchmark @end proof

end cleverbench_102_v2
