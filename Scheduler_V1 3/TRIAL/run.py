import os

from trial_app.factory import create_app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("TRIAL_PORT", "5001"))
    app.run(debug=True, port=port, use_reloader=False)
