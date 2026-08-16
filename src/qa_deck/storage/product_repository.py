"""JSON-backed storage for products."""

from pathlib import Path

from qa_deck.domain import Product
from qa_deck.storage.json_file import read_json_list, write_json_list_atomic


class ProductRepository:
    """Store products in a local JSON file."""

    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)

    def list_all(self) -> list[Product]:
        """Return all stored products."""
        return [Product.from_dict(item) for item in read_json_list(self._file_path)]

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
        if any(
            existing.name.casefold() == product.name.casefold()
            for existing in products
        ):
            raise ValueError(f"Product with name '{product.name}' already exists")

        products.append(product)
        self._save(products)

    def remove(self, product_id: str) -> Product | None:
        """Remove and return one exact Product, if it exists."""
        products = self.list_all()
        removed = next((item for item in products if item.id == product_id), None)
        if removed is None:
            return None
        self._save([item for item in products if item.id != product_id])
        return removed

    def _save(self, products: list[Product]) -> None:
        write_json_list_atomic(
            self._file_path,
            [product.to_dict() for product in products],
        )
