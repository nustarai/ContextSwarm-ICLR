import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_135

@[reducible, simp]
def implementation_precond (lst : List Int) : Prop :=
  -- !benchmark @start precond
  True
  -- !benchmark @end precond

def implementation (lst: List Int) (h_precond : implementation_precond (lst)) : (Option Int × Option Int) :=
  -- !benchmark @start code
  let negatives := lst.filter (fun x => x < 0)
  let positives := lst.filter (fun x => x > 0)
  let max_neg := if negatives.length > 0 then
    some (negatives.foldl (fun acc x => if x > acc then x else acc) negatives[0]!)
    else none
  let min_pos := if positives.length > 0 then
    some (positives.foldl (fun acc x => if x < acc then x else acc) positives[0]!)
    else none
  (max_neg, min_pos)
  -- !benchmark @end code

@[reducible, simp]
def implementation_postcond (lst : List Int) (result : (Option Int × Option Int)) (h_precond : implementation_precond (lst)) : Prop :=
  -- !benchmark @start postcond
  let (a, b) := result;
  (match a with
  | none => ¬(∃ i, i ∈ lst ∧ i < 0)
  | some a => a < 0 ∧ a ∈ lst ∧ ∀ i, i ∈ lst ∧ i < 0 → i ≤ a) ∧
  (match b with
  | none => ¬(∃ i, i ∈ lst ∧ 0 < i)
  | some b => 0 < b ∧ b ∈ lst ∧ ∀ i, i ∈ lst ∧ 0 < i → b ≤ i)
  -- !benchmark @end postcond

theorem implementation_postcond_satisfied (lst : List Int) (h_precond : implementation_precond (lst)) :
    implementation_postcond (lst) (implementation lst h_precond) h_precond := by
  -- !benchmark @start proof
  sorry
  -- !benchmark @end proof

end cleverbench_135
