"""QA Deck application package."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from flask import Flask

from qa_deck.storage import ProductRepository


def create_app(test_config: Mapping[str, object] | None = None) -> Flask:
    """Create and configure the QA Deck Flask application."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        PRODUCT_DATA_PATH=Path(app.instance_path) / "products.json",
    )

    if test_config is not None:
        app.config.from_mapping(test_config)

    product_data_path = cast(str | Path, app.config["PRODUCT_DATA_PATH"])
    app.extensions["product_repository"] = ProductRepository(product_data_path)

    from qa_deck.web.routes import web_blueprint

    app.register_blueprint(web_blueprint)
    return app
