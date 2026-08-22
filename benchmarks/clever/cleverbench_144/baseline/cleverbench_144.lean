import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_144

@[reducible, simp]
def implementation_precond (nums : List Int) : Prop :=
  True

def implementationAux (nums : List Int) : Int :=
  match nums with
  | [] => 0
  | head :: tail =>
    if head > 10 then
      let first_digit_str := head.natAbs.repr
      let first_digit :=
        if first_digit_str.length > 0 then
          (first_digit_str.toList[0]!.toNat - '0'.toNat)
        else 0
      let last_digit := head.natAbs % 10
      if first_digit % 2 == 1 && last_digit % 2 == 1 then
        1 + implementationAux tail
      else
        implementationAux tail
    else
      implementationAux tail

def implementation (nums : List Int) (h_precond : implementation_precond nums) : Int :=
  implementationAux nums

@[reducible]
def validHead (head : Int) : Bool :=
  if h : head > 10 then
    let s := head.natAbs.repr
    let firstDigit : Nat :=
      if hs : 0 < s.length then
        (s.toList[0]!.toNat - Char.toNat '0')
      else
        0
    let lastDigit : Nat := head.natAbs % 10
    (firstDigit % 2 == 1) && (lastDigit % 2 == 1)
  else
    false


@[reducible, simp]
def implementation_postcond (nums : List Int) (result : Int)
    (h_precond : implementation_precond nums) : Prop :=
  result = nums.foldr (fun head acc => if validHead head then acc + 1 else acc) 0

theorem implementation_postcond_satisfied (nums : List Int)
    (h_precond : implementation_precond nums) :
    implementation_postcond nums (implementation nums h_precond) h_precond := by
  sorry

end cleverbench_144
