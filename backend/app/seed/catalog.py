"""Fixed reference data for the synthetic shops: shops, suppliers and product lists.

Everything here is invented for the demo. Phone numbers use the obviously fake
``+91-00000-xxxxx`` pattern so none of them can belong to a real person. Prices
are illustrative rupee amounts, and GST rates are illustrative too (not tax advice).
"""

from dataclasses import dataclass
from datetime import date

CITY = "Demo City"


@dataclass(frozen=True)
class ShopSpec:
    id: str
    name: str
    shop_type: str
    owner_name: str
    staff_name: str
    locality: str
    opened_on: date
    sku_prefix: str
    customers: int
    bills_per_day: tuple[int, ...]  # choices for bills per day (sampled uniformly)


SHOPS: list[ShopSpec] = [
    ShopSpec("SHOP-001", "Sri Lakshmi Kirana Store", "kirana", "Ramesh Naidu", "Sunita Rao",
             "Gandhi Nagar", date(2014, 6, 1), "KIR", 25, (0, 1, 2, 2, 3)),
    ShopSpec("SHOP-002", "Balaji Hardware & Paints", "hardware", "Suresh Patel", "Manoj Yadav",
             "Station Road", date(2011, 3, 15), "HW", 20, (0, 0, 1, 1, 2)),
    ShopSpec("SHOP-003", "Vidya Stationery & Xerox", "stationery", "Anita Sharma", "Rahul Das",
             "College Road", date(2018, 7, 1), "ST", 20, (0, 1, 1, 1, 2)),
    ShopSpec("SHOP-004", "Nandini Dairy & Bakery", "dairy_bakery", "Kavya Reddy", "Arun Kumar",
             "Gandhi Nagar", date(2019, 1, 10), "DB", 20, (0, 1, 1, 2, 3)),
    ShopSpec("SHOP-005", "Smart Mobile Accessories", "mobile_accessories", "Imran Khan",
             "Deepak Singh", "Market Street", date(2021, 8, 20), "MOB", 15, (0, 0, 1, 1, 2)),
]  # fmt: skip


@dataclass(frozen=True)
class SupplierSpec:
    id: str
    name: str
    contact_person: str
    categories: str
    payment_terms_days: int
    lead_time_days: int
    min_order_value: int


SUPPLIERS: list[SupplierSpec] = [
    SupplierSpec("SUP-001", "Shree Balaji Traders", "Venkat Rao", "grocery,staples", 15, 2, 2000),
    SupplierSpec("SUP-002", "Annapurna Foods Distributors", "Lalitha Devi", "grocery,spices,oil", 7, 3, 1500),
    SupplierSpec("SUP-003", "Metro FMCG Agency", "Prakash Jain", "household,personal_care,snacks", 30, 4, 3000),
    SupplierSpec("SUP-004", "Deccan Hardware Distributors", "Mohammed Ali", "hardware,electrical,cement", 30, 5, 5000),
    SupplierSpec("SUP-005", "Colorline Paints Depot", "Harish Kumar", "paints", 30, 4, 5000),
    SupplierSpec("SUP-006", "Pragati Pipes & Fittings", "Sunil Verma", "plumbing", 15, 3, 2000),
    SupplierSpec("SUP-007", "Saraswati Book Depot", "Geeta Iyer", "stationery,notebooks", 30, 5, 1500),
    SupplierSpec("SUP-008", "PaperWorld Wholesale", "Rohit Agarwal", "paper,office", 15, 2, 1000),
    SupplierSpec("SUP-009", "Nandi Milk Co-operative", "Shankar Gowda", "dairy", 7, 1, 500),
    SupplierSpec("SUP-010", "Golden Crust Bakery Supplies", "Joseph Mathew", "bakery", 7, 1, 500),
    SupplierSpec("SUP-011", "TechLink Accessories Wholesale", "Arjun Mehta", "mobile_accessories,audio", 30, 6, 3000),
    SupplierSpec("SUP-012", "Volt Mobile Distributors", "Farhan Siddiqui", "mobile_accessories,chargers", 15, 4, 2000),
]  # fmt: skip


@dataclass(frozen=True)
class ProductSpec:
    name: str
    category: str
    unit: str
    cost: int
    price: int
    gst: int
    reorder_level: int
    reorder_qty: int
    supplier_id: str
    demand: float  # relative popularity inside the shop


def _p(*args: object) -> ProductSpec:
    return ProductSpec(*args)  # type: ignore[arg-type]


PRODUCTS: dict[str, list[ProductSpec]] = {
    "kirana": [
        _p("Sona Masoori Rice 25kg", "rice", "bag", 1150, 1300, 5, 4, 10, "SUP-001", 0.8),
        _p("Basmati Rice 1kg", "rice", "packet", 95, 120, 5, 10, 30, "SUP-001", 1.4),
        _p("Toor Dal 1kg", "pulses", "packet", 140, 165, 5, 8, 24, "SUP-002", 1.5),
        _p("Moong Dal 1kg", "pulses", "packet", 115, 135, 5, 6, 20, "SUP-002", 0.9),
        _p("Chana Dal 1kg", "pulses", "packet", 85, 100, 5, 6, 20, "SUP-002", 0.8),
        _p("Wheat Atta 10kg", "flour", "bag", 380, 440, 5, 5, 15, "SUP-001", 1.2),
        _p("Sugar 1kg", "staples", "packet", 40, 46, 5, 12, 40, "SUP-001", 1.8),
        _p("Iodised Salt 1kg", "staples", "packet", 18, 25, 0, 10, 30, "SUP-001", 1.0),
        _p("Sunflower Oil 1L", "oil", "pouch", 130, 150, 5, 10, 30, "SUP-002", 1.6),
        _p("Groundnut Oil 1L", "oil", "pouch", 165, 190, 5, 6, 20, "SUP-002", 0.9),
        _p("Tea Powder 250g", "beverages", "packet", 110, 130, 5, 8, 24, "SUP-003", 1.3),
        _p("Coffee Powder 200g", "beverages", "packet", 140, 165, 5, 5, 15, "SUP-003", 0.7),
        _p("Glucose Biscuits 200g", "snacks", "packet", 22, 30, 18, 15, 48, "SUP-003", 1.7),
        _p("Cream Biscuits 100g", "snacks", "packet", 18, 25, 18, 12, 48, "SUP-003", 1.2),
        _p("Instant Noodles 70g", "snacks", "packet", 12, 15, 12, 20, 60, "SUP-003", 1.6),
        _p("Turmeric Powder 100g", "spices", "packet", 25, 32, 5, 6, 20, "SUP-002", 0.7),
        _p("Red Chilli Powder 100g", "spices", "packet", 30, 40, 5, 6, 20, "SUP-002", 0.8),
        _p("Mustard Seeds 100g", "spices", "packet", 12, 18, 5, 5, 20, "SUP-002", 0.5),
        _p("Cumin Seeds 100g", "spices", "packet", 35, 45, 5, 5, 20, "SUP-002", 0.5),
        _p("Poha 500g", "breakfast", "packet", 30, 38, 5, 6, 20, "SUP-001", 0.7),
        _p("Rava 500g", "breakfast", "packet", 28, 35, 5, 6, 20, "SUP-001", 0.6),
        _p("Bath Soap 100g", "personal_care", "piece", 28, 38, 18, 12, 48, "SUP-003", 1.1),
        _p("Detergent Powder 1kg", "household", "packet", 85, 105, 18, 8, 24, "SUP-003", 1.0),
        _p("Dishwash Bar 200g", "household", "piece", 18, 25, 18, 10, 36, "SUP-003", 0.9),
        _p("Toothpaste 150g", "personal_care", "tube", 75, 95, 18, 6, 24, "SUP-003", 0.8),
        _p("Coconut Hair Oil 200ml", "personal_care", "bottle", 70, 90, 18, 5, 20, "SUP-003", 0.6),
        _p("Agarbatti Pack", "pooja", "pack", 25, 35, 5, 6, 24, "SUP-003", 0.5),
        _p("Matchbox Pack of 10", "household", "pack", 15, 20, 12, 8, 30, "SUP-003", 0.6),
        _p("Eggs Tray of 30", "eggs", "tray", 165, 195, 0, 3, 10, "SUP-001", 0.9),
    ],
    "hardware": [
        _p("OPC Cement 50kg bag", "cement", "bag", 360, 410, 28, 20, 80, "SUP-004", 1.6),
        _p("PVC Pipe 1 inch 3m", "plumbing", "piece", 160, 210, 18, 10, 40, "SUP-006", 1.0),
        _p("PVC Elbow 1 inch", "plumbing", "piece", 12, 20, 18, 25, 100, "SUP-006", 1.0),
        _p("Wire Nails 2 inch 1kg", "fasteners", "kg", 75, 95, 18, 8, 25, "SUP-004", 0.9),
        _p("Wood Screws Box of 100", "fasteners", "box", 90, 120, 18, 5, 20, "SUP-004", 0.6),
        _p("GI Binding Wire 1kg", "construction", "kg", 85, 105, 18, 8, 25, "SUP-004", 0.7),
        _p("Wall Putty 20kg", "paints", "bag", 650, 780, 18, 4, 12, "SUP-005", 0.6),
        _p("Exterior Emulsion Paint 4L", "paints", "can", 1450, 1750, 18, 3, 10, "SUP-005", 0.5),
        _p("Interior Emulsion Paint 4L", "paints", "can", 980, 1200, 18, 3, 10, "SUP-005", 0.6),
        _p("Enamel Paint 1L", "paints", "can", 320, 390, 18, 5, 15, "SUP-005", 0.6),
        _p("Paint Brush 2 inch", "paints", "piece", 45, 65, 18, 8, 30, "SUP-005", 0.7),
        _p("Paint Roller 9 inch", "paints", "piece", 120, 160, 18, 4, 15, "SUP-005", 0.4),
        _p("Sandpaper Sheet", "paints", "sheet", 8, 12, 18, 20, 100, "SUP-005", 0.5),
        _p("Epoxy Putty 100g", "adhesives", "piece", 45, 60, 18, 8, 30, "SUP-004", 0.6),
        _p("Brass Water Tap", "plumbing", "piece", 220, 290, 18, 5, 15, "SUP-006", 0.5),
        _p("Ball Valve 1 inch", "plumbing", "piece", 180, 240, 18, 4, 15, "SUP-006", 0.4),
        _p("Teflon Tape", "plumbing", "roll", 10, 20, 18, 15, 60, "SUP-006", 0.8),
        _p("LED Bulb 9W", "electrical", "piece", 65, 99, 18, 15, 50, "SUP-004", 1.0),
        _p(
            "Electrical Wire 1.5 sqmm 90m",
            "electrical",
            "coil",
            1350,
            1650,
            18,
            3,
            8,
            "SUP-004",
            0.4,
        ),
        _p("Switch 6A", "electrical", "piece", 25, 40, 18, 15, 50, "SUP-004", 0.7),
        _p("Extension Board 4-way", "electrical", "piece", 210, 280, 18, 4, 12, "SUP-004", 0.5),
        _p("Padlock 50mm", "security", "piece", 140, 190, 18, 4, 12, "SUP-004", 0.4),
        _p("Measuring Tape 5m", "tools", "piece", 90, 130, 18, 4, 12, "SUP-004", 0.4),
        _p("Hacksaw Blade", "tools", "piece", 15, 25, 18, 10, 40, "SUP-004", 0.5),
        _p("Cement Trowel", "tools", "piece", 110, 150, 18, 3, 10, "SUP-004", 0.3),
    ],
    "stationery": [
        _p("A4 Copier Paper 500 sheets", "paper", "ream", 230, 280, 12, 8, 30, "SUP-008", 1.2),
        _p("Ruled Notebook 200 pages", "notebooks", "piece", 38, 50, 12, 20, 80, "SUP-007", 1.8),
        _p("Long Notebook 172 pages", "notebooks", "piece", 45, 60, 12, 15, 60, "SUP-007", 1.4),
        _p("Blue Ball Pen Pack of 10", "pens", "pack", 50, 70, 18, 10, 30, "SUP-007", 1.5),
        _p("Black Gel Pen", "pens", "piece", 8, 12, 18, 30, 100, "SUP-007", 1.4),
        _p("HB Pencil Pack of 10", "pencils", "pack", 35, 50, 12, 8, 30, "SUP-007", 1.0),
        _p("Eraser", "pencils", "piece", 3, 5, 12, 30, 100, "SUP-007", 1.0),
        _p("Sharpener", "pencils", "piece", 4, 6, 12, 30, 100, "SUP-007", 0.9),
        _p("Geometry Box", "instruments", "piece", 95, 130, 18, 5, 20, "SUP-007", 0.6),
        _p("Scale 30cm", "instruments", "piece", 10, 15, 12, 15, 50, "SUP-007", 0.7),
        _p("Glue Stick 15g", "adhesives", "piece", 18, 25, 18, 10, 40, "SUP-007", 0.8),
        _p("White Glue 100g", "adhesives", "bottle", 22, 30, 18, 8, 30, "SUP-007", 0.6),
        _p("Sketch Pens Set of 12", "art", "set", 45, 60, 18, 6, 24, "SUP-007", 0.7),
        _p("Crayons Box of 12", "art", "box", 30, 45, 18, 6, 24, "SUP-007", 0.6),
        _p("Chart Paper", "paper", "sheet", 6, 10, 12, 30, 100, "SUP-008", 0.8),
        _p("Brown Cover Roll", "paper", "roll", 25, 35, 12, 8, 30, "SUP-008", 0.6),
        _p("Stapler", "office", "piece", 85, 120, 18, 4, 12, "SUP-008", 0.4),
        _p("Stapler Pins Box", "office", "box", 12, 18, 18, 10, 40, "SUP-008", 0.6),
        _p("Basic Calculator", "office", "piece", 180, 250, 18, 3, 10, "SUP-008", 0.3),
        _p("Highlighter", "pens", "piece", 18, 25, 18, 10, 40, "SUP-007", 0.6),
        _p("Whiteboard Marker", "pens", "piece", 22, 30, 18, 10, 40, "SUP-007", 0.6),
        _p("File Folder", "office", "piece", 12, 20, 18, 15, 50, "SUP-008", 0.6),
        _p("Envelopes Pack of 25", "office", "pack", 30, 45, 12, 6, 20, "SUP-008", 0.4),
        _p("Exam Pad", "office", "piece", 55, 80, 18, 6, 20, "SUP-007", 0.5),
        _p("Correction Pen", "pens", "piece", 25, 35, 18, 6, 20, "SUP-007", 0.4),
    ],
    "dairy_bakery": [
        _p("Toned Milk 500ml", "milk", "pouch", 25, 28, 0, 30, 100, "SUP-009", 2.5),
        _p("Full Cream Milk 500ml", "milk", "pouch", 32, 35, 0, 20, 60, "SUP-009", 1.6),
        _p("Curd 500g", "dairy", "cup", 30, 38, 5, 10, 30, "SUP-009", 1.3),
        _p("Paneer 200g", "dairy", "packet", 75, 95, 5, 6, 20, "SUP-009", 0.9),
        _p("Butter 100g", "dairy", "packet", 52, 60, 12, 6, 20, "SUP-009", 0.8),
        _p("Ghee 500ml", "dairy", "jar", 290, 340, 12, 4, 12, "SUP-009", 0.5),
        _p("Buttermilk 200ml", "dairy", "pouch", 10, 15, 0, 15, 50, "SUP-009", 1.0),
        _p("Cheese Slices 200g", "dairy", "packet", 120, 145, 12, 4, 12, "SUP-009", 0.4),
        _p("White Bread 400g", "bread", "loaf", 32, 40, 0, 10, 30, "SUP-010", 1.8),
        _p("Brown Bread 400g", "bread", "loaf", 40, 50, 0, 6, 20, "SUP-010", 0.8),
        _p("Pav Pack of 6", "bread", "pack", 25, 35, 0, 8, 25, "SUP-010", 1.0),
        _p("Rusk 300g", "bakery", "packet", 45, 60, 5, 6, 20, "SUP-010", 0.7),
        _p("Butter Cookies 200g", "bakery", "packet", 60, 90, 18, 5, 15, "SUP-010", 0.6),
        _p("Plum Cake 300g", "bakery", "piece", 110, 150, 18, 3, 10, "SUP-010", 0.4),
        _p("Veg Puff", "bakery", "piece", 12, 20, 5, 10, 30, "SUP-010", 1.2),
        _p("Cream Roll", "bakery", "piece", 15, 25, 18, 8, 25, "SUP-010", 0.8),
        _p("Birthday Cake 1kg", "cakes", "piece", 450, 650, 18, 1, 3, "SUP-010", 0.2),
        _p("Muffins Pack of 4", "bakery", "pack", 60, 90, 18, 4, 12, "SUP-010", 0.5),
        _p("Eggs Pack of 6", "eggs", "pack", 36, 45, 0, 8, 25, "SUP-009", 1.0),
        _p("Flavoured Milk 200ml", "milk", "bottle", 22, 30, 12, 10, 30, "SUP-009", 0.8),
        _p("Lassi 200ml", "dairy", "bottle", 20, 30, 5, 8, 24, "SUP-009", 0.6),
        _p("Khoa 250g", "dairy", "packet", 90, 120, 5, 3, 10, "SUP-009", 0.3),
    ],
    "mobile_accessories": [
        _p("USB-C Cable 1m", "cables", "piece", 70, 199, 18, 10, 40, "SUP-011", 1.6),
        _p("Lightning Cable 1m", "cables", "piece", 120, 299, 18, 5, 20, "SUP-011", 0.8),
        _p("Micro USB Cable 1m", "cables", "piece", 50, 149, 18, 5, 20, "SUP-011", 0.6),
        _p("20W Fast Charger", "chargers", "piece", 280, 599, 18, 5, 20, "SUP-012", 1.2),
        _p("10W Charger", "chargers", "piece", 140, 349, 18, 4, 15, "SUP-012", 0.6),
        _p("33W Charging Adapter", "chargers", "piece", 450, 899, 18, 3, 10, "SUP-012", 0.6),
        _p("Wired Earphones", "audio", "piece", 110, 299, 18, 6, 24, "SUP-011", 1.0),
        _p("Bluetooth Earbuds", "audio", "piece", 650, 1299, 18, 4, 12, "SUP-011", 0.9),
        _p("Neckband Earphones", "audio", "piece", 450, 999, 18, 3, 10, "SUP-011", 0.6),
        _p(
            "Tempered Glass Screen Guard",
            "protection",
            "piece",
            25,
            149,
            18,
            20,
            80,
            "SUP-011",
            2.0,
        ),
        _p("Silicone Back Cover", "protection", "piece", 40, 199, 18, 15, 60, "SUP-011", 1.5),
        _p("Power Bank 10000mAh", "power", "piece", 650, 1199, 18, 3, 10, "SUP-012", 0.5),
        _p("Car Charger", "chargers", "piece", 150, 349, 18, 3, 10, "SUP-012", 0.3),
        _p("Memory Card 64GB", "storage", "piece", 380, 599, 18, 4, 12, "SUP-012", 0.4),
        _p("Pendrive 32GB", "storage", "piece", 280, 449, 18, 4, 12, "SUP-012", 0.4),
        _p("OTG Adapter", "adapters", "piece", 30, 99, 18, 6, 24, "SUP-011", 0.5),
        _p("Type-C to 3.5mm Adapter", "adapters", "piece", 60, 149, 18, 5, 20, "SUP-011", 0.5),
        _p("Phone Stand", "accessories", "piece", 60, 149, 18, 4, 15, "SUP-011", 0.4),
        _p("Selfie Stick", "accessories", "piece", 180, 399, 18, 2, 8, "SUP-011", 0.3),
        _p("Mini Bluetooth Speaker", "audio", "piece", 550, 999, 18, 2, 8, "SUP-011", 0.3),
        _p("Smart Watch Strap", "accessories", "piece", 80, 249, 18, 4, 15, "SUP-011", 0.4),
        _p("Screen Cleaning Kit", "accessories", "piece", 40, 99, 18, 5, 20, "SUP-011", 0.3),
    ],
}

# Customer mix per shop type: (customer_type, share, credit_allowed share, credit limit in rupees)
CUSTOMER_MIX: dict[str, list[tuple[str, float, float, int]]] = {
    "kirana": [("household", 1.0, 0.7, 3000)],
    "hardware": [("contractor", 0.4, 1.0, 15000), ("household", 0.6, 0.4, 3000)],
    "stationery": [("institution", 0.1, 1.0, 10000), ("student", 0.6, 0.0, 0),
                   ("household", 0.3, 0.2, 2000)],
    "dairy_bakery": [("household", 1.0, 0.6, 2000)],
    "mobile_accessories": [("household", 0.9, 0.1, 2000), ("business", 0.1, 1.0, 5000)],
}  # fmt: skip

LOCALITIES = [
    "Gandhi Nagar",
    "Station Road",
    "College Road",
    "Market Street",
    "Lake View Colony",
    "Teachers Colony",
    "Old Town",
    "Green Park",
]
