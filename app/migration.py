import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from sqlalchemy import inspect, text

from .extensions import db
from .accounts import release_account
from .models import (Comment, Media, MigrationState, Post, Profile, SiteSetting,
                     User, UserMusicTrack, utcnow)
from .taxonomy import infer_category, normalize_tags


MIGRATION_NAME = 'tinydb_to_sqlite_v1'
SINGLE_COMMUNITY_MIGRATION = 'collapse_boards_to_single_community_v1'
SINGLE_FILE_PATH_MIGRATION = 'single_file_static_paths_v1'
DEACTIVATED_IDENTITY_MIGRATION = 'release_deactivated_identities_v1'
POST_TAXONOMY_MIGRATION = 'entertainment_post_taxonomy_v1'


def migrate_music_sources_schema():
    """Add remote music source columns to an existing SQLite database."""
    columns = {column['name'] for column in
               inspect(db.engine).get_columns('user_music_tracks')}
    additions = {
        'source_type': "VARCHAR(20) NOT NULL DEFAULT 'upload'",
        'source_id': "VARCHAR(64) NOT NULL DEFAULT ''",
        'stream_url': "VARCHAR(500) NOT NULL DEFAULT ''",
        'cover_url': "VARCHAR(500) NOT NULL DEFAULT ''",
        'duration_ms': "INTEGER NOT NULL DEFAULT 0",
    }
    changed = False
    for name, definition in additions.items():
        if name not in columns:
            db.session.execute(text(
                'ALTER TABLE user_music_tracks ADD COLUMN {} {}'.format(
                    name, definition)))
            changed = True
    if changed:
        db.session.commit()
    return changed


def migrate_post_music_schema():
    """Add nullable post music reference without rebuilding existing posts."""
    columns = {column['name'] for column in inspect(db.engine).get_columns('posts')}
    if 'music_track_id' in columns:
        return False
    db.session.execute(text(
        'ALTER TABLE posts ADD COLUMN music_track_id INTEGER NULL'))
    db.session.execute(text(
        'CREATE INDEX IF NOT EXISTS ix_posts_music_track_id '
        'ON posts (music_track_id)'))
    db.session.commit()
    return True


def migrate_post_taxonomy_schema():
    """Add post categories and consolidate the renamed interest tags."""
    columns = {column['name'] for column in inspect(db.engine).get_columns('posts')}
    if 'category' not in columns:
        db.session.execute(text(
            "ALTER TABLE posts ADD COLUMN category VARCHAR(20) "
            "NOT NULL DEFAULT 'daily'"))
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_posts_category ON posts (category)'))
        db.session.commit()

    if db.session.get(MigrationState, POST_TAXONOMY_MIGRATION):
        return False
    for post in Post.query.all():
        original_tags = list(post.tags or [])
        post.category = infer_category(original_tags)
        post.tags = normalize_tags(original_tags)
    db.session.add(MigrationState(name=POST_TAXONOMY_MIGRATION))
    db.session.commit()
    return True


def migrate_post_pin_schema():
    """Add the per-author profile pin flag to existing databases."""
    columns = {column['name'] for column in inspect(db.engine).get_columns('posts')}
    if 'is_pinned' in columns:
        return False
    db.session.execute(text(
        'ALTER TABLE posts ADD COLUMN is_pinned BOOLEAN NOT NULL DEFAULT 0'))
    db.session.execute(text(
        'CREATE INDEX IF NOT EXISTS ix_posts_is_pinned ON posts (is_pinned)'))
    db.session.commit()
    return True


def _table(data, name):
    return data.get(name, {}) or {}


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def migrate_tinydb(legacy_path=None):
    """Copy the legacy TinyDB JSON into SQLite exactly once.

    The source file is deliberately retained as a rollback backup.
    """
    path = Path(legacy_path or os.environ.get('CHRONOFLASK_DB_PATH', 'db.json'))
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        backup_path = path.with_name(path.name + '.pre-sqlite-backup')
        if not backup_path.exists():
            shutil.copy2(path, backup_path)

    if db.session.get(MigrationState, MIGRATION_NAME):
        return False

    if not path.exists():
        db.session.add(MigrationState(name=MIGRATION_NAME))
        db.session.commit()
        return False

    with path.open('r', encoding='utf-8') as source:
        data = json.load(source)

    auth_rows = _table(data, 'auth')
    profile_rows = _table(data, 'profiles')
    admin_rows = _table(data, 'admin')

    first_user_id = min((int(key) for key in auth_rows), default=None)
    for key, row in auth_rows.items():
        user_id = int(key)
        username = (row.get('username') or row.get('email') or
                    'legacy_user_{}'.format(user_id)).strip().lower()
        user = db.session.get(User, user_id)
        if not user:
            user = User(id=user_id, username=username,
                        password_hash=row['password_hash'],
                        role='admin' if user_id == first_user_id else 'user',
                        status='active')
            db.session.add(user)

        profile_data = next((item for item in profile_rows.values()
                             if item.get('user_id') == user_id), None)
        admin_data = next((item for item in admin_rows.values()
                           if item.get('creator_id') == user_id), {})
        if not db.session.get(Profile, user_id):
            profile_data = profile_data or {}
            db.session.add(Profile(
                user_id=user_id,
                nickname=(profile_data.get('nickname') or
                          admin_data.get('author_name') or username),
                avatar=profile_data.get('avatar', ''),
                bio=profile_data.get('bio', ''),
                background_image=profile_data.get('bg_image', ''),
                background_music=profile_data.get('bg_music', '')
            ))

    db.session.flush()

    for key, row in _table(data, 'entries').items():
        post_id = int(key)
        if db.session.get(Post, post_id):
            continue
        created_at = _parse_time(row.get('timestamp'))
        post = Post(
            id=post_id,
            author_id=row.get('creator_id') or first_user_id,
            title=row.get('title') or '',
            content=row.get('entry', ''),
            board='public',
            tags=row.get('tags') or [],
            images=row.get('images') or [],
            status='published',
            legacy_likes=row.get('likes', 0),
            created_at=created_at or utcnow(),
            updated_at=created_at or utcnow()
        )
        db.session.add(post)

    db.session.flush()

    for key, row in _table(data, 'comments').items():
        comment_id = int(key)
        if db.session.get(Comment, comment_id):
            continue
        timestamp = row.get('entry_timestamp')
        owner_id = row.get('entry_creator_id')
        candidates = Post.query.filter(Post.created_at == _parse_time(timestamp))
        if owner_id:
            candidates = candidates.filter(Post.author_id == owner_id)
        post = candidates.first()
        if not post:
            continue
        nickname = row.get('nickname', '')
        author = Profile.query.filter(Profile.nickname == nickname).first()
        db.session.add(Comment(
            id=comment_id,
            post_id=post.id,
            author_id=author.user_id if author else None,
            legacy_nickname=nickname,
            content=row.get('content', ''),
            created_at=_parse_time(row.get('created_at')) or utcnow()
        ))

    first_admin = next(iter(admin_rows.values()), {})
    if not db.session.get(SiteSetting, 'site_name'):
        db.session.add(SiteSetting(
            key='site_name',
            value=first_admin.get('chronofile_name', 'RXBlog'),
            updated_by=first_user_id
        ))

    db.session.add(MigrationState(name=MIGRATION_NAME))
    db.session.commit()
    return True


def migrate_single_community():
    """Collapse legacy board values into one inclusive community feed."""
    if db.session.get(MigrationState, SINGLE_COMMUNITY_MIGRATION):
        return False
    Post.query.filter(Post.board != 'public').update(
        {Post.board: 'public'}, synchronize_session=False)
    db.session.add(MigrationState(name=SINGLE_COMMUNITY_MIGRATION))
    db.session.commit()
    return True


def migrate_single_file_static_paths():
    """Add the missing static/uploads prefix to legacy profile media paths."""
    if db.session.get(MigrationState, SINGLE_FILE_PATH_MIGRATION):
        return False

    folders = ('avatar/', 'background/', 'music/', 'music_cover/')

    def normalize(value):
        value = (value or '').replace('\\', '/').lstrip('/')
        if value.startswith('uploads/'):
            return value
        if value.startswith(folders):
            return 'uploads/' + value
        return value

    for profile in Profile.query.all():
        profile.avatar = normalize(profile.avatar)
        profile.background_image = normalize(profile.background_image)
        profile.background_music = normalize(profile.background_music)
    for track in UserMusicTrack.query.all():
        track.audio_path = normalize(track.audio_path)
        track.cover_path = normalize(track.cover_path)
    for media in Media.query.all():
        media.file_path = normalize(media.file_path)

    db.session.add(MigrationState(name=SINGLE_FILE_PATH_MIGRATION))
    db.session.commit()
    return True


def migrate_deactivated_identities():
    """Release account names and nicknames retained by older soft deletes."""
    if db.session.get(MigrationState, DEACTIVATED_IDENTITY_MIGRATION):
        return False
    users = User.query.filter(User.status != 'active').all()
    for user in users:
        if not user.username.startswith('__deactivated_account_'):
            release_account(user.username)
            user.username = '__deactivated_account_{}__'.format(user.id)
        if user.profile and not user.profile.nickname.startswith(
                '__deactivated_user_'):
            user.profile.nickname = '__deactivated_user_{}__'.format(user.id)
    db.session.add(MigrationState(name=DEACTIVATED_IDENTITY_MIGRATION))
    db.session.commit()
    return True
