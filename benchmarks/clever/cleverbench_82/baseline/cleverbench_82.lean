import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_82

@[reducible, simp]
def implementation_precond (n : Nat) : Prop :=
  True

def implementation (n: Nat) (h_precond : implementation_precond (n)) : Nat :=
  if n == 0 then 0
  else
    let start := 10 ^ (n - 1)
    let end_val := 10 ^ n
    let rec count (num : Nat) (acc : Nat) : Nat :=
      if num ≥ end_val then acc
      else
        let str := num.repr
        let has_front_one := str.length > 0 && str.front = '1'
        let has_back_one := str.length > 0 && str.back = '1'
        let new_acc := if has_front_one || has_back_one then acc + 1 else acc
        count (num + 1) new_acc
    count start 0

def specCount (n : Nat) : Nat :=
  if n == 0 then 0
  else
    let start := 10 ^ (n - 1)
    let end_val := 10 ^ n
    let rec go (k : Nat) (acc : Nat) : Nat :=
      if k ≥ end_val then acc
      else
        let str := k.repr
        let ok : Bool :=
          (str.length > 0) && (str.front = '1' || str.back = '1')
        go (k + 1) (if ok then acc + 1 else acc)
    go start 0

@[reducible, simp]
def implementation_postcond
  (n : Nat) (result : Nat) (h_precond : implementation_precond n) : Prop :=
  0 < n → result = specCount n

theorem implementation_postcond_satisfied (n : Nat) (h_precond : implementation_precond n) :
    implementation_postcond n (implementation n h_precond) h_precond := by
  sorry

end cleverbench_82
