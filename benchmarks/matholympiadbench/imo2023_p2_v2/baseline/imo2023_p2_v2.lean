import Mathlib
import Aesop

set_option maxHeartbeats 0

open BigOperators Real Nat Topology Rat
open Affine EuclideanGeometry FiniteDimensional InnerProductSpace Module Simplex
open scoped Affine EuclideanGeometry Real InnerProductSpace

attribute [local instance] FiniteDimensional.of_fact_finrank_eq_two

variable (V : Type*) (Pt : Type*)

variable [NormedAddCommGroup V] [InnerProductSpace ℝ V] [MetricSpace Pt]

variable [NormedAddTorsor V Pt] [hd2 : Fact (finrank ℝ V = 2)]

variable [Module.Oriented ℝ V (Fin 2)]

/-!
# International Mathematical Olympiad 2023, Problem 2

Let ABC be an acute-angled triangle with AB < AC.
Let Ω be the circumcircle of ABC.
Let S be the midpoint of the arc CB of Ω containing A.
The perpendicular from A to BC meets BS at D and meets Ω again at E ≠ A.
The line through D parallel to BC meets line BE at L.
Denote the circumcircle of triangle BDL by ω.
Let ω meet Ω again at P ≠ B.
Prove that the line tangent to ω at P meets line BS on the internal angle bisector of ∠BAC.

The theorem contract below follows the corrected Compfiles formalization introduced in
`dwrensha/compfiles@9eb1eefae03f7da96911ae1551e588a5f8f63ced`.

MathOlympiadBench runs on Lean 4.9, before `Affine.Simplex.AcuteAngled` and
`Sphere.IsTangentAt` entered Mathlib. The two definitions below are exact logical
backports of the parts of those later APIs used by the corrected statement:

* `acuteTriangle` is the three-angle characterization of `Triangle.AcuteAngled`;
* `sphereIsTangentAt` states membership in the sphere and affine space, followed by
  the pointwise inner-product characterization of inclusion in `Sphere.orthRadius`.
-/

def acuteTriangle (A B C : Pt) : Prop :=
  ∠ A B C < Real.pi / 2 ∧ ∠ B C A < Real.pi / 2 ∧ ∠ C A B < Real.pi / 2

def sphereIsTangentAt
    (ω : Sphere Pt) (P : Pt) (tang : AffineSubspace ℝ Pt) : Prop :=
  P ∈ (ω : Set Pt) ∧ P ∈ tang ∧
    ∀ X ∈ tang, ⟪X -ᵥ P, P -ᵥ ω.center⟫_ℝ = 0

theorem imo2023_p1
  -- Points
  (A B C D E L S P : Pt)
  -- Circles
  (Ω ω : Sphere Pt)
  -- Lines
  (perp_A_BC prll_D_BC tang_P_ω : AffineSubspace ℝ Pt)
  -- Let ABC be an acute-angled triangle
  (h_ABC : AffineIndependent ℝ ![A, B, C])
  (h_acute_ABC : acuteTriangle V Pt A B C)
  -- with AB < AC.
  (h_AB_lt_BC : dist A B < dist A C)
  -- Let Ω be the circumcircle of ABC.
  (h_Ω : {A, B, C} ⊆ (Ω : Set Pt))
  -- Let S be the midpoint of the arc CB of Ω
  (h_S_Ω : dist S C = dist S B ∧ S ∈ (Ω : Set Pt))
  -- ... containing A.
  (h_S_A : (∡ C B S).sign = (∡ C B A).sign)
  -- The perpendicular from A to BC ...
  (h_perp_A_BC : perp_A_BC.direction ⟂ line[ℝ, B, C].direction ∧ A ∈ perp_A_BC)
  -- ... meets BS at D
  (h_D : D ∈ (perp_A_BC : Set Pt) ∩ line[ℝ, B, S])
  -- ... and meets Ω again at E ...
  (h_E : E ∈ (perp_A_BC : Set Pt) ∩ Ω)
  -- ... E ≠ A.
  (h_E_ne_A : E ≠ A)
  -- The line through D parallel to BC ...
  (h_prll_D_BC : D ∈ prll_D_BC ∧ prll_D_BC ∥ line[ℝ, B, C])
  -- ... meets line BE at L.
  (h_L : L ∈ (prll_D_BC : Set Pt) ∩ line[ℝ, B, E])
  -- Denote the circumcircle of triangle BDL by ω.
  (h_ω : {B, D, L} ⊆ (ω : Set Pt))
  -- Let ω meet Ω again at P ...
  (h_P : P ∈ (ω : Set Pt) ∩ Ω)
  -- P ≠ B.
  (h_P_ne_B : P ≠ B)
  -- Prove that the line tangent to ω at P ...
  (h_rank_tang_P_ω : finrank ℝ tang_P_ω.direction = 1)
  (h_tang_P_ω : sphereIsTangentAt V Pt ω P tang_P_ω) :
  -- meets line BS on the internal angle bisector of ∠BAC.
  ∃ X : Pt,
    X ∈ (tang_P_ω : Set Pt) ∩ line[ℝ, B, S]
    ∧ ∠ B A X = ∠ X A C
    ∧ ∠ B A X < Real.pi / 2 := by
  sorry
