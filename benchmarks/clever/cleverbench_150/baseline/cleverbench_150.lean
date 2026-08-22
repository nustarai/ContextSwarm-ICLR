import Mathlib
import Std
import Tacs

set_option maxHeartbeats 0

namespace cleverbench_150

@[reducible, simp]
def implementation_precond (scores : List Rat) (guesses : List Rat) : Prop :=
  True

def implementation (scores guesses : List Rat)
    (h_precond : implementation_precond scores guesses) : List Rat :=
  let rec aux (s : List Rat) (g : List Rat) : List Rat :=
    match s, g with
    | [], [] => []
    | s_h :: s_t, g_h :: g_t =>
      let diff := if s_h > g_h then s_h - g_h else g_h - s_h
      diff :: aux s_t g_t
    | _, _ => []
  aux scores guesses

abbrev QRat := Int × Int

@[simp] def QRat.den (q : QRat) : Int := q.2
@[simp] def QRat.num (q : QRat) : Int := q.1

def QRat.toRat (q : QRat) : Rat :=
  (q.num : Rat) / (q.den : Rat)

def allDenNonZeroB (xs : List QRat) : Bool :=
  xs.all (fun q => q.den ≠ 0)

def allDenNonZero (xs : List QRat) : Prop :=
  allDenNonZeroB xs = true


@[reducible, simp]
def implementation_postcondB
    (scores : List QRat) (guesses : List QRat) (result : List Rat)
    (h_precond :
      implementation_precond (scores.map QRat.toRat) (guesses.map QRat.toRat)) : Bool :=
  if hds : allDenNonZeroB scores = true then
    if hgs : allDenNonZeroB guesses = true then
      if hlen : scores.length = guesses.length then
        let rs : List Rat := scores.map QRat.toRat
        let gs : List Rat := guesses.map QRat.toRat
        let out : List Rat := result
        (decide (out.length = rs.length)) &&
          (List.all (List.range rs.length) (fun i =>
            let ri := rs[i]!
            let gi := gs[i]!
            let oi := out[i]!
            decide (if ri > gi then oi + gi = ri else oi + ri = gi)))
      else
        true
    else
      true
  else
    true

@[reducible, simp]
def implementation_postcond
    (scores : List QRat) (guesses : List QRat) (result : List Rat)
    (h_precond :
      implementation_precond (scores.map QRat.toRat) (guesses.map QRat.toRat)) : Prop :=
  implementation_postcondB scores guesses result h_precond = true

theorem implementation_postcond_satisfied (scores : List QRat) (guesses : List QRat)
    (h_precond :
      implementation_precond (scores.map QRat.toRat) (guesses.map QRat.toRat)) :
    implementation_postcond scores guesses
      (implementation (scores.map QRat.toRat) (guesses.map QRat.toRat) h_precond)
      h_precond := by
  sorry

end cleverbench_150
