"""Web routes for QA Deck."""

from typing import cast
from uuid import uuid4

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    url_for,
)

from qa_deck.domain import Product
from qa_deck.storage import ProductRepository

web_blueprint = Blueprint("web", __name__)


@web_blueprint.get("/")
def index() -> Response:
    """Redirect to the product list."""
    return redirect(url_for("web.product_list"))


@web_blueprint.get("/products")
def product_list() -> str:
    """Show all stored products."""
    return render_template("products/list.html", products=_repository().list_all())


@web_blueprint.route("/products/new", methods=["GET", "POST"])
def product_new() -> str | Response | tuple[str, int]:
    """Show the product form and store valid submissions."""
    if request.method == "GET":
        return render_template("products/new.html", error=None, form={})

    try:
        product = Product(
            id=str(uuid4()),
            name=request.form.get("name", ""),
            description=request.form.get("description", "").strip(),
            executable_path=_optional_text(request.form.get("executable_path", "")),
            working_directory=_optional_text(
                request.form.get("working_directory", "")
            ),
            launch_arguments=_launch_arguments(
                request.form.get("launch_arguments", "")
            ),
        )
    except ValueError:
        return (
            render_template(
                "products/new.html",
                error="Вкажіть назву продукту.",
                form=request.form,
            ),
            400,
        )

    _repository().add(product)
    return redirect(url_for("web.product_detail", product_id=product.id))


@web_blueprint.get("/products/<product_id>")
def product_detail(product_id: str) -> str:
    """Show one product or return 404 when it is missing."""
    product = _repository().get(product_id)
    if product is None:
        abort(404)

    return render_template("products/detail.html", product=product)


@web_blueprint.get("/health")
def health() -> dict[str, str]:
    """Report whether the application is running."""
    return {"status": "ok"}


def _repository() -> ProductRepository:
    return cast(
        ProductRepository,
        current_app.extensions["product_repository"],
    )


def _optional_text(value: str) -> str | None:
    stripped_value = value.strip()
    return stripped_value or None


def _launch_arguments(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]
