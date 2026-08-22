import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_89_v2

@[reducible, simp]
def implementation_precond (lst : List Int) : Prop :=
  True

def implementation (lst : List Int) (_h_precond : implementation_precond lst) : Option Int :=
  if lst.length < 2 then none
  else
    let sorted := lst.mergeSort
    let min_val := sorted[0]!
    let second_min := sorted.find? (fun x => x > min_val)
    second_min

def hasIncreasingPairB (lst : List Int) : Bool :=
  let is : List (Fin lst.length) := List.finRange lst.length
  is.any (fun i =>
    is.any (fun j =>
      decide (i.1 ≠ j.1) && decide (lst.get i < lst.get j)
    )
  )

def hasIncreasingPair (lst : List Int) : Prop :=
  hasIncreasingPairB lst = true

@[reducible, simp]
def implementation_postcond (lst : List Int) (result : Option Int)
    (_h_precond : implementation_precond lst) : Prop :=
  (result = none → hasIncreasingPairB lst = false) ∧
  (∀ r, result = some r →
    r ∈ lst ∧
      let smaller_els := lst.filter (· < r)
      0 < smaller_els.length ∧
        smaller_els.all (fun x => x = smaller_els[0]!))

theorem implementation_postcond_satisfied (lst : List Int)
    (h_precond : implementation_precond lst) :
    implementation_postcond lst (implementation lst h_precond) h_precond := by
  sorry

end cleverbench_89_v2
