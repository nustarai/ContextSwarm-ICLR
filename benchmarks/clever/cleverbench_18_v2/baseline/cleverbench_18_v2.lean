import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_18_v2

@[reducible, simp]
def implementation_precond (string : String) (substring : String) : Prop :=
  True

def implementation_postcond (string : String) (substring : String) (result : Nat)
    (_h : implementation_precond string substring) : Prop :=
  let lenS := string.length
  let lenT := substring.length
  (lenT = 0 → result = lenS) ∧
  (lenT ≠ 0 →
    (lenS < lenT → result = 0) ∧
    (lenS = lenT →
      ((string = substring ↔ result = 1) ∧
       (substring ≠ string ↔ result = 0))) ∧
    (lenT < lenS →
      let upper := lenS - lenT
      let occ := ((List.range (upper + 1)).filter (fun i =>
        decide ((string.drop i).take lenT = substring))).length
      result = occ))

def implementation_specCount (string : String) (substring : String) : Nat :=
  let lenS := string.length
  let lenT := substring.length
  if lenT = 0 then
    lenS
  else if lenS < lenT then
    0
  else
    let upper := lenS - lenT
    ((List.range (upper + 1)).filter (fun i =>
      decide ((string.drop i).take lenT = substring))).length

def implementation (string : String) (substring : String) (_h : implementation_precond string substring) : Nat :=
  implementation_specCount string substring

theorem implementation_postcond_satisfied (string : String) (substring : String)
    (h_precond : implementation_precond string substring) :
    implementation_postcond string substring (implementation string substring h_precond) h_precond := by
  sorry

end cleverbench_18_v2
