"""Shared decision arithmetic. Unknown costs never become zeroes.

Amounts are explicit observations or assumptions, never inferred by this module.
Capacity is an assumption-based monthly steady-state scenario, not a sales forecast.
"""

from decimal import Decimal, ROUND_HALF_UP

COST_FIELDS = (
    "inbound_shipping_cost_eur", "buyer_fee_eur", "platform_fee_eur",
    "payment_fee_eur", "seller_shipping_cost_eur", "packaging_cost_eur",
    "repair_cost_eur", "labour_cost_eur", "holding_cost_eur",
    "return_loss_reserve_eur", "overhead_cost_eur", "other_variable_cost_eur",
    "tax_cost_eur",
)


def amount(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
        raise ValueError(f"{name} must be a finite number between 0 and 1000000000")
    return number


def money(value):
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def unit_economics(acquisition_cost_eur, sale_price_eur, costs, required_costs=COST_FIELDS):
    """Evaluate every declared cost, keeping incomplete calculations unknown."""
    if not isinstance(costs, dict) or set(costs) - set(required_costs):
        raise ValueError("unsupported cost fields")
    buy = amount(acquisition_cost_eur, "acquisition_cost_eur")
    sale = amount(sale_price_eur, "sale_price_eur")
    values = {key: amount(costs.get(key), key) for key in required_costs}
    unknown = [key for key, value in values.items() if value is None]
    if buy is None:
        unknown.insert(0, "acquisition_cost_eur")
    known = (buy or Decimal(0)) + sum((value for value in values.values() if value is not None), Decimal(0))
    complete = not unknown and sale is not None
    profit = sale - known if complete else None
    return dict(status="COMPLETE" if complete else "INCOMPLETE", unknown_costs=unknown,
                sale_price_known=sale is not None, known_cost_eur=money(known),
                total_cost_eur=money(known) if not unknown else None,
                net_profit_eur=money(profit),
                net_margin_pct=float(profit / sale) if complete and sale else None,
                roi_on_total_cost=float(profit / known) if complete and known else None)


def monthly_capacity(capital_per_unit_eur, labour_hours_per_unit, cash_cycle_days,
                     cash_budget_eur, monthly_labour_hours, storage_units,
                     source_units_per_month, sale_units_per_month, profit_per_unit_eur):
    """The limiting explicit assumption determines a monthly throughput rate.

    Cash and storage use an assumed 30-day month and one inventory cycle. The
    result does not model a cold start, returns, purchasing lead times or the
    interaction between multiple product families sharing the same resources.
    """
    inputs = dict(capital_per_unit_eur=capital_per_unit_eur,
                  labour_hours_per_unit=labour_hours_per_unit, cash_cycle_days=cash_cycle_days,
                  cash_budget_eur=cash_budget_eur, monthly_labour_hours=monthly_labour_hours,
                  storage_units=storage_units, source_units_per_month=source_units_per_month,
                  sale_units_per_month=sale_units_per_month)
    values = {key: amount(value, key) for key, value in inputs.items()}
    missing = [key for key, value in values.items() if value is None]
    result = dict(basis="user_assumptions", status="INCOMPLETE", unknown_inputs=missing,
                  units_per_month=None, monthly_profit_eur=None, limiting_constraints=[],
                  note="Single-product steady-state scenario; capacities across products share resources and cannot be added.")
    if missing:
        return result
    capital, hours, days = (values[key] for key in
                            ("capital_per_unit_eur", "labour_hours_per_unit", "cash_cycle_days"))
    if not capital or not days:
        raise ValueError("capital_per_unit_eur and cash_cycle_days must be positive")
    if values["storage_units"] != values["storage_units"].to_integral_value():
        raise ValueError("storage_units must be an integer")
    concurrent_cash = int(values["cash_budget_eur"] / capital)
    limits = dict(cash=Decimal(concurrent_cash) * 30 / days,
                  storage=values["storage_units"] * 30 / days,
                  sourcing=values["source_units_per_month"], demand=values["sale_units_per_month"])
    if hours:
        limits["labour"] = values["monthly_labour_hours"] / hours
    units = min(limits.values())
    # Inventory must fit at least one unit before a fractional monthly turnover
    # can be meaningful (e.g. a cycle longer than a month).
    concurrent = min(concurrent_cash, int(values["storage_units"]))
    if not concurrent:
        units = 0
    profit = None
    if profit_per_unit_eur is not None:
        if isinstance(profit_per_unit_eur, bool) or not isinstance(profit_per_unit_eur, (int, float, Decimal)):
            raise ValueError("profit_per_unit_eur must be finite")
        profit = Decimal(str(profit_per_unit_eur))
        if not profit.is_finite():
            raise ValueError("profit_per_unit_eur must be finite")
    minimum = min(limits.values())
    result.update(status="COMPLETE", units_per_month=round(float(units), 4),
                  whole_units_per_month_floor=int(units), max_concurrent_units=concurrent,
                  monthly_profit_eur=money(profit * units) if profit is not None else None,
                  limiting_constraints=[key for key, value in limits.items() if value == minimum],
                  constraint_units_per_month={key: float(value) for key, value in limits.items()})
    return result
