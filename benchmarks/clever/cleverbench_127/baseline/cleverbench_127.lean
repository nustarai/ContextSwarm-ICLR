import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_127

@[reducible, simp]
def implementation_precond (arr : List Int) : Prop :=
  -- !benchmark @start precond
  True
  -- !benchmark @end precond

def implementation (arr: List Int) (h_precond : implementation_precond (arr)) : Option Int :=
  -- !benchmark @start code
  if arr.length == 0 then none
  else
    let magnitude_sum := (arr.map (fun x => x.natAbs)).sum
    let neg_count := (arr.filter (fun x => x < 0)).length
    let has_zero := arr.contains 0
    if has_zero then some 0
    else if neg_count % 2 == 1 then
      some (-magnitude_sum)
    else
      some magnitude_sum
  -- !benchmark @end code

@[reducible, simp]
def implementation_postcond (arr : List Int) (result : Option Int) (h_precond : implementation_precond (arr)) : Prop :=
  -- !benchmark @start postcond
  match result with
  | none => arr.length = 0
  | some result =>
    let magnitude_sum := (arr.map (fun x => Int.ofNat x.natAbs)).sum;
    let neg_count_odd := (arr.filter (fun x => x < 0)).length % 2 = 1;
    let has_zero := 0 ∈ arr;
    (result < 0 ↔ (neg_count_odd ∧ ¬has_zero)
      ∧ result = magnitude_sum * -1) ∧
    (0 < result ↔ (¬neg_count_odd ∧ ¬has_zero)
      ∧ result = magnitude_sum) ∧
    (result = 0 ↔ has_zero)
  -- !benchmark @end postcond

theorem implementation_postcond_satisfied (arr : List Int) (h_precond : implementation_precond (arr)) :
    implementation_postcond (arr) (implementation arr h_precond) h_precond := by
  -- !benchmark @start proof
  sorry
  -- !benchmark @end proof

end cleverbench_127
