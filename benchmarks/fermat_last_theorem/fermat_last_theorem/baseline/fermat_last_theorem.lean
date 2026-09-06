import Mathlib

set_option autoImplicit false

theorem fermat_last_theorem
    (n : ℕ) (hn : 3 ≤ n)
    (a b c : ℕ)
    (ha : 0 < a) (hb : 0 < b) (hc : 0 < c) :
    a ^ n + b ^ n ≠ c ^ n := by
  sorry
