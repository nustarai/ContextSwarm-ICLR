import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_3

@[reducible, simp]
def implementation_precond (operations : List Int) : Prop :=
  -- !benchmark @start precond
  True
  -- !benchmark @end precond

def implementation (operations: List Int) (h_precond : implementation_precond (operations)) : Bool :=
  -- !benchmark @start code
  let rec check (ops : List Int) (acc : Int) : Bool :=
    match ops with
    | []        => false
    | op :: ops' =>
      let new_acc := acc + op
      if new_acc < 0 then true else check ops' new_acc
  check operations 0
  -- !benchmark @end code

@[reducible, simp]
def implementation_postcond (operations : List Int) (result : Bool) (h_precond : implementation_precond (operations)) : Prop :=
  -- !benchmark @start postcond
  let below_zero_condition := ∃ i, i ≤ operations.length ∧
  (operations.take i).sum < 0;
  if result then below_zero_condition else ¬below_zero_condition
  -- !benchmark @end postcond

theorem implementation_postcond_satisfied (operations : List Int) (h_precond : implementation_precond (operations)) :
    implementation_postcond (operations) (implementation operations h_precond) h_precond := by
  -- !benchmark @start proof
  sorry
  -- !benchmark @end proof

end cleverbench_3
