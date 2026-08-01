"""Run the QA Deck development server."""

from qa_deck import create_app

app = create_app()


if __name__ == "__main__":
    app.run()
