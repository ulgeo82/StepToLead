import json
import math
import unittest

from pydantic import ValidationError

from app.services.business_economics import EconomicsInput, calculateBusinessEconomics


class EconomicsTests(unittest.TestCase):
    def calculate(self, **values):
        return calculateBusinessEconomics(EconomicsInput(**values))

    def test_reference_scenario_all_metrics(self):
        expected = {
            "revenuePerCustomer": 50000, "grossProfitPerCustomer": 20000,
            "currentRevenue": 2000000, "currentGrossProfit": 800000,
            "currentOperatingProfit": 300000, "operatingMargin": .15,
            "breakEvenCustomers": 25, "breakEvenCustomersRounded": 25,
            "breakEvenRevenue": 1250000, "safetyCustomers": 15, "safetyMargin": .375,
            "targetRevenue": 3000000, "targetGrossProfit": 1200000,
            "potentialOperatingProfit": 700000, "revenueGrowth": 1000000,
            "potentialProfitGrowth": 400000, "additionalCustomers": 20,
            "budgetLoad": .3, "profitAfterInvestment": 210000,
            "operatingMarginAfterInvestment": .105, "profitReserveAfterInvestment": .7,
        }
        result = self.calculate()
        self.assertEqual(result.metrics, expected)
        self.assertEqual(result.status, "TEST_READY")

    def test_rounding_does_not_change_safety_or_revenue(self):
        m = self.calculate(fixed_costs=510001).metrics
        self.assertEqual(m["breakEvenCustomersRounded"], 26)
        self.assertAlmostEqual(m["breakEvenCustomers"], 25.50005)
        self.assertAlmostEqual(m["safetyMargin"], (40 - 25.50005) / 40)
        self.assertEqual(m["breakEvenRevenue"], 1275002.5)

    def test_fractional_purchases(self):
        self.assertEqual(self.calculate(purchases_per_customer=1.5).metrics["revenuePerCustomer"], 75000)

    def test_zero_divisors_json_safe(self):
        for values in ({"gross_margin": 0}, {"average_order_value": 0}, {"purchases_per_customer": 0}, {"current_customers": 0}, {"fixed_costs": 800000}):
            result = self.calculate(**values)
            json.dumps(result.model_dump(), allow_nan=False)
            self.assertEqual(result.status, "NOT_READY")
            self.assertTrue(result.hints)
        self.assertIsNone(self.calculate(gross_margin=0).metrics["breakEvenCustomers"])
        self.assertIsNone(self.calculate(current_customers=0).metrics["safetyMargin"])
        self.assertIsNone(self.calculate(fixed_costs=800000).metrics["budgetLoad"])

    def test_negative_profit_and_budget_exhaustion(self):
        self.assertEqual(self.calculate(fixed_costs=900000).status, "NOT_READY")
        self.assertEqual(self.calculate(growth_budget=300000).status, "NOT_READY")
        self.assertEqual(self.calculate(growth_budget=400000).status, "NOT_READY")

    def test_limited_thresholds(self):
        self.assertEqual(self.calculate(growth_budget=150000).status, "TEST_READY")
        self.assertEqual(self.calculate(growth_budget=150001).status, "LIMITED")
        # 5% margin exactly passes; below fails, even when load stays under 50%.
        self.assertEqual(self.calculate(fixed_costs=600000, growth_budget=100000).status, "TEST_READY")
        self.assertEqual(self.calculate(fixed_costs=650000, growth_budget=50001).status, "LIMITED")

    def test_no_fixed_costs_and_no_budget(self):
        result = self.calculate(fixed_costs=0, growth_budget=0)
        self.assertEqual(result.metrics["safetyMargin"], 1)
        self.assertEqual(result.metrics["profitReserveAfterInvestment"], 1)
        self.assertTrue(result.hints)

    def test_target_is_capacity_not_forecast(self):
        result = self.calculate(target_customers=40)
        self.assertEqual(result.metrics["potentialProfitGrowth"], 0)
        self.assertEqual(result.status, "TEST_READY")

    def test_invalid_inputs(self):
        for key, value in [("average_order_value", -1), ("gross_margin", 101), ("growth_budget", math.inf), ("fixed_costs", math.nan), ("target_customers", 39), ("current_customers", 1.5), ("purchases_per_customer", ""), ("growth_budget", None)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                EconomicsInput(**{key: value})


if __name__ == "__main__":
    unittest.main()
