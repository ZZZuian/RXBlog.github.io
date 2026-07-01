from datetime import datetime, timezone

from .extensions import db

DEFAULT_AVATAR_PATH = 'images/default-avatar.jpg'


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    profile = db.relationship('Profile', back_populates='user', uselist=False,
                              cascade='all, delete-orphan')
    posts = db.relationship('Post', back_populates='author',
                            cascade='all, delete-orphan')

    @property
    def display_name(self):
        if self.status != 'active':
            return '该用户已注销'
        if self.profile:
            return self.profile.nickname
        return self.username


class Profile(db.Model):
    __tablename__ = 'profiles'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    nickname = db.Column(db.String(30), nullable=False, index=True)
    avatar = db.Column(db.String(255), nullable=False,
                       default=DEFAULT_AVATAR_PATH)
    bio = db.Column(db.String(500), nullable=False, default='')
    background_image = db.Column(db.String(255), nullable=False, default='')
    background_music = db.Column(db.String(255), nullable=False, default='')
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow,
                           onupdate=utcnow)

    user = db.relationship('User', back_populates='profile')


class Post(db.Model):
    __tablename__ = 'posts'

    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                          index=True)
    title = db.Column(db.String(200), nullable=False, default='')
    content = db.Column(db.Text, nullable=False)
    board = db.Column(db.String(20), nullable=False, default='public', index=True)
    category = db.Column(db.String(20), nullable=False, default='daily', index=True)
    is_pinned = db.Column(db.Boolean, nullable=False, default=False, index=True)
    tags = db.Column(db.JSON, nullable=False, default=list)
    images = db.Column(db.JSON, nullable=False, default=list)
    status = db.Column(db.String(20), nullable=False, default='published', index=True)
    legacy_likes = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow,
                           onupdate=utcnow)
    music_track_id = db.Column(
        db.Integer,
        db.ForeignKey('user_music_tracks.id', ondelete='SET NULL'),
        nullable=True, index=True)

    author = db.relationship('User', back_populates='posts')
    comments = db.relationship('Comment', back_populates='post',
                               cascade='all, delete-orphan')
    likes = db.relationship('PostLike', back_populates='post',
                            cascade='all, delete-orphan')
    image_items = db.relationship('PostImage', back_populates='post',
                                  cascade='all, delete-orphan',
                                  order_by='PostImage.sort_order')
    music_track = db.relationship('UserMusicTrack',
                                  foreign_keys=[music_track_id])

    @property
    def likes_count(self):
        return self.legacy_likes + len(self.likes)

    @property
    def timestamp(self):
        return self.created_at.strftime('%Y-%m-%d %H:%M:%S')

    @property
    def entry(self):
        return self.content

    @property
    def display_images(self):
        stored = [item.media.file_path for item in self.image_items
                  if item.media]
        return stored + list(self.images or [])

    @property
    def excerpt(self):
        compact = ' '.join(self.content.split())
        return compact[:140] + ('…' if len(compact) > 140 else '')


class Media(db.Model):
    __tablename__ = 'media'

    id = db.Column(db.Integer, primary_key=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                            nullable=False, index=True)
    media_type = db.Column(db.String(20), nullable=False, default='image')
    file_path = db.Column(db.String(255), unique=True, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    uploader = db.relationship('User')


class PostImage(db.Model):
    __tablename__ = 'post_images'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False,
                        index=True)
    media_id = db.Column(db.Integer, db.ForeignKey('media.id'), nullable=False,
                         unique=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    post = db.relationship('Post', back_populates='image_items')
    media = db.relationship('Media', cascade='all, delete')


class PostLike(db.Model):
    __tablename__ = 'post_likes'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    post = db.relationship('Post', back_populates='likes')
    user = db.relationship('User')


class Comment(db.Model):
    __tablename__ = 'comments'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False,
                        index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    legacy_nickname = db.Column(db.String(30), nullable=False, default='')
    content = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='visible')
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    post = db.relationship('Post', back_populates='comments')
    author = db.relationship('User')

    @property
    def nickname(self):
        if self.author:
            return self.author.display_name
        return self.legacy_nickname or '历史访客'


class UserMusicTrack(db.Model):
    __tablename__ = 'user_music_tracks'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                        index=True)
    title = db.Column(db.String(100), nullable=False)
    artist = db.Column(db.String(100), nullable=False, default='')
    audio_path = db.Column(db.String(255), nullable=False)
    cover_path = db.Column(db.String(255), nullable=False, default='')
    source_type = db.Column(db.String(20), nullable=False, default='upload')
    source_id = db.Column(db.String(64), nullable=False, default='')
    stream_url = db.Column(db.String(500), nullable=False, default='')
    cover_url = db.Column(db.String(500), nullable=False, default='')
    duration_ms = db.Column(db.Integer, nullable=False, default=0)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship('User', backref='music_tracks')


class GuestbookMessage(db.Model):
    __tablename__ = 'guestbook_messages'

    id = db.Column(db.Integer, primary_key=True)
    profile_user_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                          nullable=False)
    content = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='visible')
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    profile_user = db.relationship('User', foreign_keys=[profile_user_id],
                                   backref='guestbook_messages')
    author = db.relationship('User', foreign_keys=[author_id])

    @property
    def nickname(self):
        if self.author:
            return self.author.display_name
        return '匿名访客'


class SiteSetting(db.Model):
    __tablename__ = 'site_settings'

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='')
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow,
                           onupdate=utcnow)


class MigrationState(db.Model):
    __tablename__ = 'migration_states'

    name = db.Column(db.String(100), primary_key=True)
    completed_at = db.Column(db.DateTime, nullable=False, default=utcnow)
