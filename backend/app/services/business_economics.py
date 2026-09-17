"""Единственный источник формул. Все денежные показатели относятся к месяцу."""
from decimal import Decimal, ROUND_CEILING
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EconomicsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    average_order_value: float = Field(default=50000, ge=0, le=1e12)
    purchases_per_customer: float = Field(default=1, ge=0, le=1e6)
    gross_margin: float = Field(default=40, ge=0, le=100)
    fixed_costs: float = Field(default=500000, ge=0, le=1e12)
    current_customers: int = Field(default=40, ge=0, le=1_000_000_000)
    target_customers: int = Field(default=60, ge=0, le=1_000_000_000)
    growth_budget: float = Field(default=90000, ge=0, le=1e12)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.target_customers < self.current_customers:
            raise ValueError("Целевое число клиентов не может быть меньше текущего")
        return self


class EconomicsResult(BaseModel):
    metrics: dict[str, float | None]
    status: Literal["NOT_READY", "LIMITED", "TEST_READY"]
    hints: list[str]
    formula_version: str = "1.0"


def calculateBusinessEconomics(data: EconomicsInput) -> EconomicsResult:
    """Считает без округления промежуточных величин; доли возвращает как 0..1.

    None означает, что делитель равен нулю и показатель не определён.
    Округляется вверх только отдельный показатель отображения безубыточности.
    """
    v = {key: Decimal(str(value)) for key, value in data.model_dump().items()}
    m: dict[str, Decimal | None] = {}
    def divide(numerator, denominator):
        return numerator / denominator if denominator else None
    m["revenuePerCustomer"] = v["average_order_value"] * v["purchases_per_customer"]
    m["grossProfitPerCustomer"] = m["revenuePerCustomer"] * v["gross_margin"] / 100
    m["currentRevenue"] = m["revenuePerCustomer"] * v["current_customers"]
    m["currentGrossProfit"] = m["grossProfitPerCustomer"] * v["current_customers"]
    m["currentOperatingProfit"] = m["currentGrossProfit"] - v["fixed_costs"]
    m["operatingMargin"] = divide(m["currentOperatingProfit"], m["currentRevenue"])
    m["breakEvenCustomers"] = divide(v["fixed_costs"], m["grossProfitPerCustomer"])
    be = m["breakEvenCustomers"]
    m["breakEvenCustomersRounded"] = be.to_integral_value(rounding=ROUND_CEILING) if be is not None else None
    m["breakEvenRevenue"] = be * m["revenuePerCustomer"] if be is not None else None
    m["safetyCustomers"] = v["current_customers"] - be if be is not None else None
    m["safetyMargin"] = divide(m["safetyCustomers"], v["current_customers"]) if be is not None else None
    m["targetRevenue"] = m["revenuePerCustomer"] * v["target_customers"]
    m["targetGrossProfit"] = m["grossProfitPerCustomer"] * v["target_customers"]
    m["potentialOperatingProfit"] = m["targetGrossProfit"] - v["fixed_costs"]
    m["revenueGrowth"] = m["targetRevenue"] - m["currentRevenue"]
    m["potentialProfitGrowth"] = m["potentialOperatingProfit"] - m["currentOperatingProfit"]
    m["additionalCustomers"] = v["target_customers"] - v["current_customers"]
    m["budgetLoad"] = divide(v["growth_budget"], m["currentOperatingProfit"])
    m["profitAfterInvestment"] = m["currentOperatingProfit"] - v["growth_budget"]
    m["operatingMarginAfterInvestment"] = divide(m["profitAfterInvestment"], m["currentRevenue"])
    m["profitReserveAfterInvestment"] = divide(m["profitAfterInvestment"], m["currentOperatingProfit"])
    if m["currentOperatingProfit"] <= 0 or m["safetyMargin"] is None or m["safetyMargin"] <= 0 or m["profitAfterInvestment"] <= 0:
        status = "NOT_READY"
    elif m["operatingMarginAfterInvestment"] < Decimal("0.05") or m["budgetLoad"] > Decimal("0.5"):
        status = "LIMITED"
    else:
        status = "TEST_READY"
    hints = []
    if not m["grossProfitPerCustomer"]:
        hints.append("Без валовой прибыли на клиента точку безубыточности определить нельзя. Проверьте чек, покупки и маржу.")
    if not m["currentRevenue"]:
        hints.append("При нулевой выручке операционная маржа не определена.")
    if not v["current_customers"]:
        hints.append("При нуле текущих клиентов запас прочности в процентах не определён.")
    if m["currentOperatingProfit"] <= 0:
        hints.append("Текущая прибыль не положительна: тестовый бюджет не обеспечен прибылью бизнеса.")
    if not v["growth_budget"]:
        hints.append("Бюджет равен нулю: оценена устойчивость бизнеса, но сам тест ещё не профинансирован.")
    return EconomicsResult(metrics={key: float(value) if value is not None else None for key, value in m.items()}, status=status, hints=hints)
