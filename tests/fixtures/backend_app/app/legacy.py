from flask import Flask

flask_app = Flask(__name__)


@flask_app.route("/legacy/ping", methods=["POST"])
def ping():
    # planted: mutating Flask route, no auth -> missing_auth
    return "pong"
