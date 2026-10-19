# flask_app.py — точка входа веб-админки Caspian College.
#
# PythonAnywhere (WSGI): `from flask_app import app as application`.
# Вся логика — в пакете cms/ (см. cms/__init__.py и DEPLOY.md).
# Локально: `python flask_app.py` (порт 5001) или `flask --app flask_app run`.

import os

from flask import Flask

import cms

app = Flask(__name__)
cms.register_all(app)

if __name__ == '__main__':
    app.run(debug=os.environ.get('CMS_DEBUG') == '1', port=int(os.environ.get('PORT', '5001')))
