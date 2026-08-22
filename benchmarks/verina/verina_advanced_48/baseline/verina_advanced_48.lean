import Mathlib
import Tacs
set_option maxHeartbeats 0

namespace verina_advanced_48

@[reducible, simp]
def mergeSort_precond (list : List Int) : Prop :=
  True

def mergeSort (list : List Int) (h_precond : mergeSort_precond (list)) : List Int :=

  let rec insert (x : Int) (sorted : List Int) : List Int :=
    match sorted with
    | [] => [x]
    | y :: ys =>
        if x ≤ y then
          x :: sorted
        else
          y :: insert x ys
  termination_by sorted.length

  let rec sort (l : List Int) : List Int :=
    match l with
    | [] => []
    | x :: xs =>
        let sortedRest := sort xs
        insert x sortedRest
  termination_by l.length

  sort list

@[reducible, simp]
def mergeSort_postcond (list : List Int) (result: List Int) (h_precond : mergeSort_precond (list)) : Prop :=
  List.Pairwise (· ≤ ·) result ∧ List.isPerm list result

theorem mergeSort_spec_satisfied (list: List Int) (h_precond : mergeSort_precond (list)) :
    mergeSort_postcond (list) (mergeSort (list) h_precond) h_precond := by sorry

end verina_advanced_48
