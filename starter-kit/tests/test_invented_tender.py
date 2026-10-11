"""A whole invented tender priced by hand from the brief's bid rules, field by field.

The golden check only proves the calculator right on the public scenario's numbers; a rule read slightly wrong can
still give the same figure there. This tender is invented (every reference, key and amount), exercises every pricing
rule at once with different numbers, and every expected value below is worked out from the rule text in the comment
next to it, not read off calculator.py. Conventions the brief leaves to the websites follow the public data: a metal
index rate is EUR per gram of surcharge mass; the bonus base is quantity x undiscounted EUR base price;
surcharge_per_unit reports the mass surcharge plus setup per piece (rule 25), all before discount.
"""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calculator import Authorities, BonusBand, Commitment  # noqa: E402
from test_calculator import D, REVISION, assigned, bid, condition, june, material, need, pos, qa  # noqa: E402


def invented_tender():
    positions = [
        pos("01.01", 10, "REQ-1"),
        pos("01.02", 15, "REQ-2"),
        pos("01.03", 20, "REQ-2"),
        pos("01.04", 8, "REQ-3"),
        pos("01.05", 3, "REQ-4", is_set=True),
    ]
    requirements = {
        "REQ-1": need("REQ-1", bore_mm=40, stroke_mm=160, usable_travel=True, reserve_key="RES-MAIN",
                      pressure_bar=160, mounting="flange", certification="CERT-X", catalogue_group="CAT-1"),
        "REQ-2": need("REQ-2", bore_mm=60, stroke_mm=291, usable_travel=True, reserve_key="RES-MAIN",
                      pressure_bar=200, mounting="clevis", certification=None, catalogue_group="CAT-2"),
        "REQ-3": need("REQ-3", bore_mm=80, stroke_mm=400, usable_travel=False,
                      pressure_bar=250, mounting="trunnion", certification="CERT-Y", catalogue_group="CAT-3"),
        "REQ-4": need("REQ-4", bore_mm=100, stroke_mm=500, usable_travel=False,
                      pressure_bar=250, mounting="foot", certification=None, catalogue_group="CAT-4", pieces_per_set=4),
    }
    clarifications = [
        qa("Q-1", june(2), quantity_changes=[("01.02", 25, False)], amendments=[(REVISION, "REQ-2", "stroke", "280")]),
        qa("Q-2", june(4), quantity_changes=[("01.02", 99, False)]),
        qa("Q-3", june(6), withdraws=["Q-2"]),
        assigned({
            "01.01": {"condition_group": "CG-BOTH", "packaging_tariff": "PKG-1", "setup_pool": "POOL-1",
                      "project_bonus": "BON-1"},
            "01.02": {"condition_group": "CG-BASE", "packaging_tariff": "PKG-1", "delivery_commitment": "DC-B-FAST",
                      "setup_pool": "POOL-1", "project_bonus": "BON-1"},
            "01.03": {"condition_group": "CG-NONE", "packaging_tariff": "PKG-1", "delivery_commitment": "DC-B-TIGHT"},
            "01.04": {"packaging_tariff": "PKG-1"},
            "01.05": {"condition_group": "CG-BOTH", "packaging_tariff": "PKG-1", "project_bonus": "BON-1"},
        }, fees={"01.01": "20.00"}),
    ]
    materials = [
        material("MAT-A", bore_mm=40, stroke_mm=180, pressure_bar=160, mounting="flange", certification="CERT-X",
                 catalogue_group="CAT-1", carton_size=4, allocation_mass_g=D(200), surcharge_mass_g=D(50),
                 index_code="IDX-1", stock=20, conditions=[condition((1, "10000.00"), price_unit=100)]),
        material("MAT-B", bore_mm=60, stroke_mm=300, pressure_bar=200, mounting="clevis", certification=None,
                 catalogue_group="CAT-2", carton_size=10, allocation_mass_g=D(120), surcharge_mass_g=D(40),
                 index_code="IDX-1", stock=40, conditions=[condition((1, "6000.00"), (20, "5000.00"), price_unit=100)]),
        material("MAT-C", bore_mm=80, stroke_mm=400, pressure_bar=250, mounting="trunnion", certification="CERT-Y",
                 catalogue_group="CAT-3", carton_size=5, allocation_mass_g=D(300), surcharge_mass_g=D(0),
                 index_code=None, stock=10,
                 conditions=[condition((1, "200.00"), currency="CHF", fx_key="FX-CHF", price_unit=1)]),
        material("MAT-D", bore_mm=100, stroke_mm=500, pressure_bar=250, mounting="foot", certification=None,
                 catalogue_group="CAT-4", carton_size=6, allocation_mass_g=D(400), surcharge_mass_g=D(100),
                 index_code="IDX-1", stock=15, conditions=[condition((1, "85.00"), (10, "75.00"), price_unit=1)]),
    ]
    authorities = Authorities(
        reserves={"RES-MAIN": (REVISION, 10)},
        discount_percent=D(10), discount_min_order=D("4000.00"),
        clauses={"CG-BOTH": "base_and_surcharge", "CG-BASE": "base_only", "CG-NONE": "none"},
        index_rates={"IDX-1": D("0.10")},
        tariffs={"PKG-1": D("4.00")},
        commitments={"DC-B-FAST": Commitment("MAT-B", 2, 50), "DC-B-TIGHT": Commitment("MAT-B", 3, 30)},
        fx={"FX-CHF": D("0.95")},
        pools={"POOL-1": (D("100.00"), "largest_remainder")},
        bonus={"BON-1": [BonusBand(D(0), D(1000), D(1)), BonusBand(D(1000), None, D(2))]},
    )
    return bid(positions, materials, clarifications, requirements, authorities)


class InventedTenderPricedByHand(unittest.TestCase):
    """Rules 1-27 on one invented tender. Shared hand work:

    Quantities (1, 13, 17): 01.01 10; 01.02 25 (Q-2's 99 withdrawn by Q-3, Q-1's 25 stands); 01.03 20; 01.04 8;
    01.05 3 Satz x 4 = 12 pieces.
    Base EUR per piece (4, 22; scales per position, rule 15): MAT-A 10000.00/100 = 100.00; MAT-B at 25 and at 20 pieces
    both reach the 20 scale: 5000.00/100 = 50.00; MAT-C 200.00 CHF x 0.95 = 190.00 EUR; MAT-D at 12 reaches the 10
    scale: 75.00.
    Discount (16): undiscounted goods 10x100 + 25x50 + 20x50 + 8x190 + 12x75 = 1000 + 1250 + 1000 + 1520 + 900 =
    5670.00 >= 4000.00, so the approved 10 % applies.
    Mass surcharge per piece (28, rate EUR/g): MAT-A 50 g x 0.10 = 5.00; MAT-B 40 x 0.10 = 4.00; MAT-C no index = 0;
    MAT-D 100 x 0.10 = 10.00.
    Packaging (23, 4.00 per carton): 01.01 ceil(10/4)=3 -> 12.00; 01.02 ceil(25/10)=3 -> 12.00; 01.03 ceil(20/10)=2 ->
    8.00; 01.04 ceil(8/5)=2 -> 8.00; 01.05 ceil(12/6)=2 -> 8.00.
    Shared setup (25): POOL-1 = 100.00 over 01.01 (10 x 200 g = 2000) and 01.02 (25 x 120 g = 3000): 40.00 and 60.00.
    01.01 also has its ordinary fee 20.00: setup 60.00; 01.02 setup 60.00.
    Project bonus (26): members 01.01, 01.02, 01.05, goods 1000 + 1250 + 900 = 3150.00; 1 % of the first 1000 = 10.00
    plus 2 % of 2150 = 43.00 -> 53.00. Shares in cents of 5300: 1682.54 / 2103.17 / 1514.29 -> floors 1682 + 2103 +
    1514 = 5299, the 1 cent left goes to the largest remainder (.54, 01.01): 16.83 / 21.03 / 15.14.
    Delivery (15, 24): MAT-B pooled 45 <= DC-B-FAST's 50 -> 2 weeks for 01.02; > DC-B-TIGHT's 30 and > stock 40 ->
    production 6 weeks for 01.03; the others are covered by stock: 1 week.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = invented_tender()
        cls.lines = {line.position: line for line in cls.result.lines}

    def field(self, position, name):
        return getattr(self.lines[position], name)

    def test_decision_discount_and_no_omission(self):
        self.assertEqual((self.result.decision, self.result.discount_percent, self.result.omitted), ("bid", D(10), []))

    def test_base_unit_prices_before_discount(self):
        self.assertEqual([self.field(p, "base_unit_price") for p in sorted(self.lines)],
                         [D("100.00"), D("50.00"), D("50.00"), D("190.00"), D("75.00")])

    def test_surcharge_per_unit_is_mass_surcharge_plus_setup_per_piece_before_discount(self):
        # 01.01 5.00 + 60/10 = 11.00; 01.02 4.00 + 60/25 = 6.40; 01.03 4.00; 01.04 0.00; 01.05 10.00
        self.assertEqual([self.field(p, "surcharge_per_unit") for p in sorted(self.lines)],
                         [D("11.00"), D("6.40"), D("4.00"), D("0.00"), D("10.00")])

    def test_packaging_per_unit(self):
        # 12/10 = 1.20; 12/25 = 0.48; 8/20 = 0.40; 8/8 = 1.00; 8/12 = 0.666.. -> 0.67
        self.assertEqual([self.field(p, "packaging_per_unit") for p in sorted(self.lines)],
                         [D("1.20"), D("0.48"), D("0.40"), D("1.00"), D("0.67")])

    def test_rebate_total_per_position(self):
        self.assertEqual([self.field(p, "rebate_total") for p in sorted(self.lines)],
                         [D("16.83"), D("21.03"), D("0.00"), D("0.00"), D("15.14")])

    def test_final_unit_prices_round_once_at_the_end(self):
        # Rule 27: discounted base + discounted surcharge (per clause) + (setup + cartons - bonus) / pieces, unrounded:
        # 01.01 both:      90.00 + 4.50 + (60 + 12 - 16.83)/10 = 94.50 + 5.517  = 100.017 -> 100.02
        # 01.02 base only: 45.00 + 4.00 + (60 + 12 - 21.03)/25 = 49.00 + 2.0388 = 51.0388 -> 51.04
        # 01.03 none:      50.00 + 4.00 + 8/20                 = 54.40
        # 01.04 default base only: 171.00 + 0 + 8/8            = 172.00
        # 01.05 both:      67.50 + 9.00 + (8 - 15.14)/12       = 76.50 - 0.595  = 75.905 -> 75.91 (half up)
        self.assertEqual([self.field(p, "unit_price") for p in sorted(self.lines)],
                         [D("100.02"), D("51.04"), D("54.40"), D("172.00"), D("75.91")])

    def test_total_is_the_sum_of_quantity_times_rounded_unit_price(self):
        # 10x100.02 + 25x51.04 + 20x54.40 + 8x172.00 + 12x75.91 = 1000.20 + 1276.00 + 1088.00 + 1376.00 + 910.92
        self.assertEqual(self.result.total_net, D("5651.12"))

    def test_materials_quantities_and_lead_times(self):
        self.assertEqual([(p, self.field(p, "material_number"), self.field(p, "quantity"), self.field(p, "lead_time_weeks"))
                          for p in sorted(self.lines)],
                         [("01.01", "MAT-A", 10, 1), ("01.02", "MAT-B", 25, 2), ("01.03", "MAT-B", 20, 6),
                          ("01.04", "MAT-C", 8, 1), ("01.05", "MAT-D", 12, 1)])


if __name__ == "__main__":
    unittest.main()
