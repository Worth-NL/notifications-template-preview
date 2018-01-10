import os

import pytest

from app import create_app


@pytest.fixture(scope='session')
def app():
    os.environ['TEMPLATE_PREVIEW_API_KEY'] = "my-secret-key"
    yield create_app()


@pytest.fixture
def client(app):
    with app.test_request_context(), app.test_client() as client:
        yield client


@pytest.fixture
def preview_post_body():
    return {
        'letter_contact_block': '123',
        'template': {
            'subject': 'letter subject',
            'content': 'letter content with ((placeholder))',
        },
        'values': {'placeholder': 'abc'},
        'dvla_org_id': '001',
    }


@pytest.fixture
def auth_header():
    return {'Authorization': 'Token my-secret-key'}
