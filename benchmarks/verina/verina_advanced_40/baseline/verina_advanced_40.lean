import Mathlib
import Tacs
set_option maxHeartbeats 0

namespace verina_advanced_40

@[reducible]
def maxOfList_precond (lst : List Nat) : Prop :=
  lst ≠ []  -- Ensure the list is non-empty

def maxOfList (lst : List Nat) (h_precond : maxOfList_precond (lst)) : Nat :=
  let rec helper (lst : List Nat) : Nat :=
    match lst with
    | [] => 0  -- technically shouldn't happen if input is always non-empty
    | [x] => x
    | x :: xs =>
      let maxTail := helper xs
      if x > maxTail then x else maxTail
  helper lst

@[reducible]
def maxOfList_postcond (lst : List Nat) (result: Nat) (h_precond : maxOfList_precond (lst)) : Prop :=
  result ∈ lst ∧ ∀ x ∈ lst, x ≤ result

theorem maxOfList_spec_satisfied (lst: List Nat) (h_precond : maxOfList_precond (lst)) :
    maxOfList_postcond (lst) (maxOfList (lst) h_precond) h_precond := by sorry

end verina_advanced_40
