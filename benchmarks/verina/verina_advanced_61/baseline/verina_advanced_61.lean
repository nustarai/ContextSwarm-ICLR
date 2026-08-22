import Mathlib
import Tacs
set_option maxHeartbeats 0

@[reducible]
def productExceptSelf_precond (nums : List Int) : Prop :=
  True

def computepref (nums : List Int) : List Int :=
  nums.foldl (fun acc x => acc ++ [acc.getLast! * x]) [1]

def computeSuffix (nums : List Int) : List Int :=
  let revSuffix := nums.reverse.foldl (fun acc x => acc ++ [acc.getLast! * x]) [1]
  revSuffix.reverse

def productExceptSelf (nums : List Int) (h_precond : productExceptSelf_precond (nums)) : List Int :=
  let n := nums.length
  if n = 0 then []
  else
    let pref := computepref nums  -- length = n + 1, where prefix[i] = product of nums[0 ... i-1]
    let suffix := computeSuffix nums  -- length = n + 1, where suffix[i] = product of nums[i ... n-1]
    List.range n |>.map (fun i => pref[i]! * suffix[i+1]!)

def List.myprod : List Int → Int
  | [] => 1
  | x :: xs => x * xs.myprod

@[reducible]
def productExceptSelf_postcond (nums : List Int) (result: List Int) (h_precond : productExceptSelf_precond (nums)) : Prop :=
  nums.length = result.length ∧
  (List.range nums.length |>.all (fun i =>
    result[i]! = some (((List.take i nums).myprod) * ((List.drop (i+1) nums).myprod))))

theorem productExceptSelf_spec_satisfied (nums: List Int) (h_precond : productExceptSelf_precond (nums)) :
    productExceptSelf_postcond (nums) (productExceptSelf (nums) h_precond) h_precond := by sorry
