from flask import Flask, jsonify

from . import db
from .customers import bp as customers_bp
from .orders import bp as orders_bp
from .products import bp as products_bp
from .summary import bp as summary_bp
from .validation import ApiError


def create_app(db_path):
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    db.init(app)
    for bp in (products_bp, customers_bp, orders_bp, summary_bp):
        app.register_blueprint(bp)

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify(e.body), e.status

    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({"error": "няма такъв ресурс"}), 404

    @app.errorhandler(405)
    def not_allowed(_e):
        return jsonify({"error": "методът не е позволен"}), 405

    return app
