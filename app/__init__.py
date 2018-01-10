import os

from flask import Flask
from flask_httpauth import HTTPTokenAuth

from app import version  # noqa


LOGO_FILENAMES = {
    '001': 'hm-government.png',
    '002': 'opg.png',
    '003': 'dwp.png',
    '004': 'geo.png',
    '005': 'ch.png',
    '006': 'dwp-welsh.png',
    '007': 'dept-for-communities.png',
    '008': 'mmo.png',
    '500': 'hm-land-registry.png',
}


def load_config(application):
    application.config['API_KEY'] = os.environ['TEMPLATE_PREVIEW_API_KEY']

    application.config['LOGO_FILENAMES'] = LOGO_FILENAMES


def create_app():
    application = Flask(
        __name__,
        static_url_path='/static',
        static_folder='../static'
    )

    load_config(application)

    from app.preview import preview_blueprint
    from app.status import status_blueprint
    application.register_blueprint(status_blueprint)
    application.register_blueprint(preview_blueprint)

    @auth.verify_token
    def verify_token(token):
        return token == application.config['API_KEY']

    return application


auth = HTTPTokenAuth(scheme='Token')
