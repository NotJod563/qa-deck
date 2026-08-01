"""JSON-backed storage for products."""

import json
from pathlib import Path
from typing import cast

from qa_deck.domain import Product


class ProductRepository:
    """Store products in a local JSON file."""

    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)

    def list_all(self) -> list[Product]:
        """Return all stored products."""
        if not self._file_path.exists():
            return []

        with self._file_path.open(encoding="utf-8") as file:
            data = cast(list[dict[str, object]], json.load(file))

        return [Product.from_dict(item) for item in data]

    def get(self, product_id: str) -> Product | None:
        """Return the product with the exact id, if it exists."""
        return next(
            (product for product in self.list_all() if product.id == product_id),
            None,
        )

    def add(self, product: Product) -> None:
        """Add a product unless its id is already stored."""
        products = self.list_all()
        if any(existing.id == product.id for existing in products):
            raise ValueError(f"Product with id '{product.id}' already exists")

        products.append(product)
        self._save(products)

    def _save(self, products: list[Product]) -> None:
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        with self._file_path.open("w", encoding="utf-8") as file:
            json.dump(
                [product.to_dict() for product in products],
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")
