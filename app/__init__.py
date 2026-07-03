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
    MAX_VIDEO_SIZE=30 * 1024 * 1024,
    MAX_POST_VIDEOS=1,
    DEFAULT_NAME='RXBlog',
    DEFAULT_AUTHOR='Chronologist',
    SEND_FILE_MAX_AGE_DEFAULT=3600,
    AI_COMMENT_ENABLED=os.environ.get('AI_COMMENT_ENABLED', 'true').lower()
                       in ('1', 'true', 'yes', 'on'),
    AI_COMMENT_PROBABILITY=float(os.environ.get('AI_COMMENT_PROBABILITY', '0.4')),
    AI_API_KEY=os.environ.get('AI_API_KEY', ''),
    AI_BASE_URL=os.environ.get('AI_BASE_URL', 'https://api.openai.com/v1'),
    AI_MODEL=os.environ.get('AI_MODEL', 'gpt-4o-mini'),
    AI_REQUEST_TIMEOUT=float(os.environ.get('AI_REQUEST_TIMEOUT', '12')),
    AI_COMMENT_MAX_ATTEMPTS=int(os.environ.get('AI_COMMENT_MAX_ATTEMPTS', '2')),
    AI_COMMENT_USER_DAILY_LIMIT=int(os.environ.get('AI_COMMENT_USER_DAILY_LIMIT', '10')),
    AI_COMMENT_GLOBAL_MINUTE_LIMIT=int(os.environ.get('AI_COMMENT_GLOBAL_MINUTE_LIMIT', '20'))
)

db.init_app(app)
csrf.init_app(app)
bootstrap = Bootstrap(app)

with app.app_context():
    from .migration import (ensure_ai_bot, ensure_performance_indexes,
                            migrate_ai_legacy_posts,
                            migrate_single_community,
                            migrate_deactivated_identities,
                            migrate_legacy_times_to_utc,
                            migrate_music_sources_schema,
                            migrate_post_pin_schema,
                            migrate_post_taxonomy_schema,
                            migrate_single_file_static_paths, migrate_tinydb,
                            migrate_ai_comment_schema)
    db.create_all()
    migrate_ai_comment_schema()
    migrate_music_sources_schema()
    migrate_post_pin_schema()
    migrate_tinydb()
    migrate_legacy_times_to_utc()
    migrate_post_taxonomy_schema()
    migrate_single_community()
    migrate_single_file_static_paths()
    migrate_deactivated_identities()
    migrate_ai_legacy_posts()
    ensure_ai_bot()
    ensure_performance_indexes()

from app.admin import admin
from app.auth import auth
from app.main import main
from app.user import user

app.register_blueprint(admin, url_prefix='/admin')
app.register_blueprint(auth, url_prefix='/admin')
app.register_blueprint(main, url_prefix='/')
app.register_blueprint(user, url_prefix='/')

from app.main import errors

from .time_utils import format_china_time
app.jinja_env.filters['localtime'] = format_china_time
