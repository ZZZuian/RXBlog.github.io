import os
from pathlib import Path

from flask import Flask
from flask_bootstrap import Bootstrap

from .extensions import db, csrf


app = Flask(__name__)

project_root = Path(__file__).resolve().parent.parent
default_database = (project_root / 'community.db').as_posix()
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'you-will-never-guess'),
    SQLALCHEMY_DATABASE_URI=os.environ.get(
        'CHRONOFLASK_DATABASE_URI', 'sqlite:///{}'.format(default_database)),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,
    MAX_IMAGE_SIZE=5 * 1024 * 1024,
    MAX_POST_IMAGES=9,
    DEFAULT_NAME='Chronoflask',
    DEFAULT_AUTHOR='Chronologist'
)

db.init_app(app)
csrf.init_app(app)
bootstrap = Bootstrap(app)

with app.app_context():
    from .migration import migrate_single_community, migrate_tinydb
    db.create_all()
    migrate_tinydb()
    migrate_single_community()

from app.admin import admin
from app.auth import auth
from app.main import main
from app.user import user

app.register_blueprint(admin, url_prefix='/admin')
app.register_blueprint(auth, url_prefix='/admin')
app.register_blueprint(main, url_prefix='/')
app.register_blueprint(user, url_prefix='/')

from app.main import errors
