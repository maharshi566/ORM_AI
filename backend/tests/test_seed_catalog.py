"""The shop catalogue: 50 distinct shops that the data generator and knowledge base share."""

from collections import Counter

from app.seed.catalog import CUSTOMER_MIX, LOCALITIES, PRODUCTS, SHOPS, SUPPLIERS


def test_there_are_fifty_shops_with_an_even_mix_of_types() -> None:
    assert len(SHOPS) == 50
    assert Counter(shop.shop_type for shop in SHOPS) == {
        "kirana": 15,
        "dairy_bakery": 10,
        "hardware": 9,
        "stationery": 8,
        "mobile_accessories": 8,
    }


def test_shop_ids_are_numbered_in_order() -> None:
    assert [shop.id for shop in SHOPS] == [f"SHOP-{n:03d}" for n in range(1, 51)]


def test_shops_and_people_are_distinct() -> None:
    assert len({shop.name for shop in SHOPS}) == 50
    people = [shop.owner_name for shop in SHOPS] + [shop.staff_name for shop in SHOPS]
    assert len(set(people)) == 100  # nobody is the owner or staff of two shops


def test_the_first_five_shops_are_unchanged() -> None:
    # The planted edge cases and the hand-written shop profiles depend on these.
    assert [(s.id, s.name, s.shop_type, s.customers) for s in SHOPS[:5]] == [
        ("SHOP-001", "Sri Lakshmi Kirana Store", "kirana", 25),
        ("SHOP-002", "Balaji Hardware & Paints", "hardware", 20),
        ("SHOP-003", "Vidya Stationery & Xerox", "stationery", 20),
        ("SHOP-004", "Nandini Dairy & Bakery", "dairy_bakery", 20),
        ("SHOP-005", "Smart Mobile Accessories", "mobile_accessories", 15),
    ]


def test_every_shop_uses_known_types_localities_and_suppliers() -> None:
    supplier_ids = {supplier.id for supplier in SUPPLIERS}
    for shop in SHOPS:
        assert shop.shop_type in PRODUCTS and shop.shop_type in CUSTOMER_MIX
        assert shop.locality in LOCALITIES
        assert shop.customers >= 12 and any(shop.bills_per_day)
        assert shop.opened_on.year <= 2023
    for products in PRODUCTS.values():
        assert {product.supplier_id for product in products} <= supplier_ids
