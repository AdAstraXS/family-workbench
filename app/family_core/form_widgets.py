from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import forms


class CleanDecimalInput(forms.NumberInput):
    def __init__(self, *args, fixed_places=None, **kwargs):
        self.fixed_places = fixed_places
        super().__init__(*args, **kwargs)

    def format_value(self, value):
        if value in (None, ""):
            return ""
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return value
        if self.fixed_places is not None:
            quantum = Decimal("1").scaleb(-self.fixed_places)
            amount = amount.quantize(quantum, rounding=ROUND_HALF_UP)
            return f"{amount:.{self.fixed_places}f}"
        rendered = format(amount, "f").rstrip("0").rstrip(".")
        return rendered or "0"


def apply_decimal_widgets(form, *, money_fields=()):
    money_fields = set(money_fields)
    for name, field in form.fields.items():
        if not isinstance(field, forms.DecimalField):
            continue
        attrs = dict(field.widget.attrs)
        attrs["class"] = attrs.get("class", "form-control")
        fixed_places = 2 if name in money_fields else None
        attrs["step"] = "0.01" if fixed_places == 2 else "any"
        field.widget = CleanDecimalInput(attrs=attrs, fixed_places=fixed_places)
