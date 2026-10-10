"""Tool schemas are rewritten into the JSON Schema subset every provider accepts."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.portable_schema import portable_parameters, portable_tools


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=3, max_length=20)
    quantity: int = Field(ge=1, le=100)


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str  # a field that happens to share a JSON Schema keyword's name
    lines: list[Line] = Field(min_length=1, max_length=5)
    notes: str | None = Field(default=None, max_length=50)
    status: Literal["open", "resolved"] | None = None
    deliver_on: date
    price: Decimal = Field(gt=0, decimal_places=2)
    limit: int = Field(default=20, description="How many to show")


def test_nested_models_optional_fields_and_hints() -> None:
    schema = portable_parameters(Order.model_json_schema())
    props = schema["properties"]

    assert schema["type"] == "object"
    assert schema["required"] == ["title", "lines", "deliver_on", "price"]
    assert "additionalProperties" not in str(schema) and "$ref" not in str(schema)
    assert props["title"] == {"type": "string"}
    assert props["lines"]["items"]["properties"]["quantity"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    }
    assert props["lines"]["items"]["required"] == ["product_id", "quantity"]
    assert props["notes"] == {"type": "string", "maxLength": 50}
    assert props["status"] == {"type": "string", "enum": ["open", "resolved"]}
    assert props["deliver_on"] == {"type": "string", "description": "Date as YYYY-MM-DD."}
    assert props["price"] == {"type": "number", "description": "Must be greater than 0."}
    assert props["limit"]["description"] == "How many to show Default: 20."


def test_the_model_still_validates_what_the_portable_schema_allows() -> None:
    """Looser is safe: the registry checks every call against the full model."""
    call = {
        "title": "Reorder",
        "lines": [{"product_id": "KIR-001", "quantity": 2}],
        "deliver_on": "2026-10-12",
        "price": 49.5,
    }

    order = Order.model_validate(call)

    assert order.price == Decimal("49.5") and order.limit == 20


def test_only_function_parameters_change() -> None:
    tools = [
        {"type": "function", "function": {"name": "x", "parameters": Line.model_json_schema()}},
        {"type": "web_search"},
    ]

    converted = portable_tools(tools)

    assert converted[1] == {"type": "web_search"}
    assert converted[0]["function"]["name"] == "x"
    assert "title" not in converted[0]["function"]["parameters"]
    assert tools[0]["function"]["parameters"]["additionalProperties"] is False  # not mutated
