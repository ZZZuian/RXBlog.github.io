import json
import importlib
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


_temp_dir = tempfile.TemporaryDirectory()
_root = Path(_temp_dir.name)
os.environ['CHRONOFLASK_DATABASE_URI'] = 'sqlite:///{}'.format(
    (_root / 'test-community.db').as_posix())
os.environ['CHRONOFLASK_DB_PATH'] = str(_root / 'missing-legacy.json')

from sqlalchemy import inspect, select, text
from PIL import Image

from app import app
from app.extensions import db
from app.migration import (AI_BOT_USERNAME, ensure_ai_bot,
                           migrate_legacy_times_to_utc, migrate_tinydb)
from app.models import (AICommentAttempt, Comment, Media, MigrationState, Post, PostImage,
                        PostLike, Profile, SiteSetting, User, UserMusicTrack)
from app.netease import NeteaseMusicError, query_netease_song
from app.services.ai_comment_service import (AICommentError,
                                             AICommentService)
from app.time_utils import format_china_time


class CommunityPlatformTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                          SECRET_KEY='phase-one-test-key')
        with app.app_context():
            db.drop_all()
            db.create_all()
        self.upload_dir = Path(tempfile.mkdtemp(dir=_root))
        app.config['UPLOAD_FOLDER'] = str(self.upload_dir)
        self.client = app.test_client()

    def tearDown(self):
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def register(self, nickname):
        response = self.client.post('/admin/register', data={
            'nickname': nickname,
            'password': 'Secret1!',
            'submit': '创建账号'
        }, follow_redirects=True)
        with app.app_context():
            account = Profile.query.filter_by(nickname=nickname).one().user.username
        return account, response

    def login(self, account):
        return self.client.post('/admin/login', data={
            'account': account,
            'password': 'Secret1!',
            'submit': '登录'
        }, follow_redirects=True)

    def logout(self):
        return self.client.get('/admin/logout', follow_redirects=True)

    def publish(self, title, content, tags=''):
        return self.client.post('/post/new', data={
            'title': title, 'content': content,
            'tags': tags, 'submit': '发布博文'
        }, follow_redirects=True)

    @staticmethod
    def image_file(filename='image.png', image_format='PNG', color='purple'):
        stream = BytesIO()
        Image.new('RGB', (32, 24), color=color).save(stream,
                                                          format=image_format)
        stream.seek(0)
        return stream, filename

    @staticmethod
    def video_file(filename='clip.mp4'):
        if filename.lower().endswith('.webm'):
            data = b'\x1a\x45\xdf\xa3' + b'webm-test-payload'
        else:
            data = b'\x00\x00\x00\x18ftypisom' + b'mp4-test-payload'
        return BytesIO(data), filename

    def test_public_feed_interaction_and_author_permissions(self):
        register_page = self.client.get('/admin/register').get_data(as_text=True)
        self.assertNotIn('name="account"', register_page)
        self.assertIn('不少于6位，包含数字和特殊字符', register_page)

        alice_account, alice_registration = self.register('Alice')
        bob_account, _ = self.register('Bob')
        self.assertEqual(alice_account, '000001')
        self.assertEqual(bob_account, '000002')
        self.assertIn('您的账号是 000001',
                      alice_registration.get_data(as_text=True))
        login_page = self.client.get('/admin/login').get_data(as_text=True)
        self.assertIn('请输入账号，如 000001', login_page)
        self.assertIn('不少于6位，包含数字和特殊字符', login_page)

        with app.app_context():
            sequence = db.session.get(SiteSetting, 'account_sequence')
            sequence.value = '999999'
            db.session.commit()
        large_account, _ = self.register('Charlie')
        self.assertEqual(large_account, '1000000')

        self.login(alice_account)
        self.publish('Alice title', 'Alice public post', tags='one')
        with app.app_context():
            alice = User.query.filter_by(username=alice_account).one()
            bob = User.query.filter_by(username=bob_account).one()
            alice_post = Post.query.filter_by(author_id=alice.id).one()
            alice_post_id = alice_post.id
            alice_timestamp = alice_post.timestamp
            alice_day = format_china_time(
                alice_post.created_at, '%Y-%m-%d')
            self.assertEqual(alice.role, 'admin')
            self.assertEqual(alice.status, 'active')
            self.assertEqual(bob.role, 'user')
        self.logout()

        self.login(bob_account)
        self.publish('Bob title', 'Bob public post', tags='two')

        # Community visibility: Bob sees Alice, and guests see both authors.
        bob_home = self.client.get('/').get_data(as_text=True)
        self.assertIn('Alice public post', bob_home)
        self.assertIn('Bob public post', bob_home)
        self.assertEqual(self.client.get(
            '/post/{}'.format(alice_post_id)).status_code, 200)
        self.assertEqual(self.client.get(
            '/admin/rename_chronofile').status_code, 403)
        self.assertIn('Alice public post', self.client.get(
            '/tags/one').get_data(as_text=True))
        self.assertIn('Alice public post', self.client.get(
            '/day/{}'.format(alice_day)).get_data(as_text=True))
        legacy_redirect = self.client.get(
            '/timestamp/{}'.format(alice_timestamp), follow_redirects=False)
        self.assertEqual(legacy_redirect.status_code, 301)
        self.assertTrue(legacy_redirect.headers['Location'].endswith(
            '/post/{}'.format(alice_post_id)))

        comment_response = self.client.post(
            '/post/{}'.format(alice_post_id),
            data={'content': 'Bob comments on Alice', 'submit': '发表评论'},
            follow_redirects=True)
        self.assertEqual(comment_response.status_code, 200)
        like_response = self.client.post(
            '/api/like/{}'.format(alice_post_id), json={'action': 'like'})
        self.assertEqual(like_response.status_code, 200)
        self.assertTrue(like_response.get_json()['liked'])

        # Visibility is public, mutation permission remains author-only.
        self.assertEqual(self.client.get(
            '/post/{}/edit'.format(alice_post_id)).status_code, 403)
        self.assertEqual(self.client.post(
            '/post/{}/delete'.format(alice_post_id),
            data={'submit': '删除记录'}).status_code, 403)
        self.logout()

        guest_home = self.client.get('/').get_data(as_text=True)
        self.assertIn('Alice public post', guest_home)
        self.assertIn('Bob public post', guest_home)
        self.assertEqual(self.client.get(
            '/post/{}'.format(alice_post_id)).status_code, 200)

        with app.app_context():
            comment = Comment.query.filter_by(post_id=alice_post_id).one()
            self.assertEqual(comment.author_id, bob.id)
            self.assertIsNotNone(db.session.get(PostLike,
                                                (bob.id, alice_post_id)))
            self.assertNotIn('pagination', inspect(db.engine).get_table_names())

        self.login(alice_account)
        edit_response = self.client.post(
            '/post/{}/edit'.format(alice_post_id),
            data={'title': 'Alice edited title',
                  'content': 'Alice edited public post',
                  'tags': 'one, edited', 'submit': '保存修改'},
            follow_redirects=True)
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn('Alice edited public post',
                      edit_response.get_data(as_text=True))

    def test_password_reset_requires_original_password(self):
        account, _ = self.register('Password Owner')
        reset_page = self.client.get('/admin/reset_password').get_data(
            as_text=True)
        self.assertIn('name="original_password"', reset_page)

        wrong = self.client.post('/admin/reset_password', data={
            'account': account,
            'original_password': 'Wrong1!',
            'new_password': 'Changed2!',
            'verify_password': 'Changed2!',
            'submit': '重置密码'
        }, follow_redirects=True)
        self.assertIn('账号或密码错误', wrong.get_data(as_text=True))

        old_login = self.client.post('/admin/login', data={
            'account': account, 'password': 'Secret1!', 'submit': '登录'
        }, follow_redirects=False)
        self.assertEqual(old_login.status_code, 302)
        self.client.get('/admin/logout')

        changed = self.client.post('/admin/reset_password', data={
            'account': account,
            'original_password': 'Secret1!',
            'new_password': 'Changed2!',
            'verify_password': 'Changed2!',
            'submit': '重置密码'
        }, follow_redirects=True)
        self.assertIn('密码已重置成功', changed.get_data(as_text=True))

        rejected_old = self.client.post('/admin/login', data={
            'account': account, 'password': 'Secret1!', 'submit': '登录'
        }, follow_redirects=False)
        self.assertEqual(rejected_old.status_code, 200)
        accepted_new = self.client.post('/admin/login', data={
            'account': account, 'password': 'Changed2!', 'submit': '登录'
        }, follow_redirects=False)
        self.assertEqual(accepted_new.status_code, 302)
        self.assertEqual(self.client.get(
            '/admin/reset_password/not-a-token').status_code, 404)

    def test_entertainment_categories_and_tag_aliases(self):
        account, _ = self.register('Category Author')
        self.login(account)
        response = self.client.post('/post/new', data={
            'title': '兴趣归档',
            'content': '娱乐向分类测试',
            'category': 'game',
            'tags': '原神, 独立游戏, 现场摄影, Vtuber',
            'submit': '发布博文'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            author = User.query.filter_by(username=account).one()
            post = Post.query.filter_by(author_id=author.id).one()
            user_id = author.id
            self.assertEqual(post.category, 'game')
            self.assertEqual(post.tags, ['游戏', '摄影', 'Vtuber'])
            default_cover = post.cover_image
            self.assertTrue(default_cover.startswith('images/post-covers/'))
            self.assertEqual(default_cover, post.cover_image)

        profile = self.client.get('/user/{}'.format(user_id)).get_data(
            as_text=True)
        self.assertIn('兴趣分区', profile)
        self.assertIn('不分区浏览所有板块帖子', profile)
        self.assertIn('光影相册', profile)
        self.assertIn('#游戏', profile)
        self.assertIn(default_cover, profile)
        profile_rail = profile.split('class="profile-rail"', 1)[1].split(
            '</aside>', 1)[0]
        self.assertIn('profile-guestbook', profile_rail)
        filtered = self.client.get(
            '/user/{}?category=game'.format(user_id)).get_data(as_text=True)
        self.assertIn('兴趣归档', filtered)
        empty = self.client.get(
            '/user/{}?category=live'.format(user_id)).get_data(as_text=True)
        self.assertNotIn('兴趣归档', empty)

    def test_profile_pin_is_unique_and_photo_wall_is_category_scoped(self):
        account, _ = self.register('Pinned Author')
        self.login(account)
        game_response = self.client.post('/post/new', data={
            'title': '置顶游戏记录', 'content': '带图但不属于相册',
            'category': 'game', 'tags': '游戏',
            'images': [self.image_file('game.png', color='red')],
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(game_response.status_code, 200)
        photo_response = self.client.post('/post/new', data={
            'title': '相册照片', 'content': '只允许这张进入照片墙',
            'category': 'photo', 'tags': '摄影',
            'images': [self.image_file('photo.png', color='blue')],
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(photo_response.status_code, 200)

        with app.app_context():
            author = User.query.filter_by(username=account).one()
            game = Post.query.filter_by(title='置顶游戏记录').one()
            photo = Post.query.filter_by(title='相册照片').one()
            user_id = author.id
            game_id = game.id
            game_path = game.display_images[0]
            photo_path = photo.display_images[0]
            self.assertFalse(game.is_pinned)
            self.assertFalse(photo.is_pinned)

        pin_response = self.client.post(
            '/post/{}/pin'.format(game_id), follow_redirects=True)
        self.assertEqual(pin_response.status_code, 200)
        pin_page = pin_response.get_data(as_text=True)
        self.assertLess(pin_page.index('编辑记录'), pin_page.index('取消置顶'))
        with app.app_context():
            self.assertTrue(db.session.get(Post, game_id).is_pinned)

        profile = self.client.get('/user/{}'.format(user_id)).get_data(
            as_text=True)
        self.assertIn('置顶博文', profile)
        wall = profile.split('class="profile-photo-wall"', 1)[1].split(
            'class="side-card-link"', 1)[0]
        self.assertIn(photo_path, wall)
        self.assertNotIn(game_path, wall)

        replacement = self.client.post('/post/new', data={
            'title': '新的置顶', 'content': '替换此前置顶',
            'category': 'daily', 'tags': '',
            'submit': '发布博文'
        }, follow_redirects=True)
        self.assertEqual(replacement.status_code, 200)
        with app.app_context():
            replacement_id = Post.query.filter_by(title='新的置顶').one().id

        replacement_pin = self.client.post(
            '/post/{}/pin'.format(replacement_id), follow_redirects=True)
        self.assertEqual(replacement_pin.status_code, 200)
        with app.app_context():
            self.assertFalse(db.session.get(Post, game_id).is_pinned)
            self.assertTrue(Post.query.filter_by(title='新的置顶').one().is_pinned)

    def test_legacy_tinydb_migration_is_idempotent(self):
        legacy_path = _root / 'legacy-fixture.json'
        legacy_data = {
            'auth': {'7': {'username': 'legacy', 'password_hash': 'hash'}},
            'admin': {'1': {'creator_id': 7,
                            'chronofile_name': 'Legacy Community',
                            'author_name': 'Legacy Author'}},
            'profiles': {},
            'entries': {'12': {
                'entry': 'Migrated post',
                'timestamp': '2026-06-29 10:00:00',
                'tags': ['legacy'], 'creator_id': 7, 'likes': 2,
                'board': 'third'}},
            'comments': {'3': {
                'entry_timestamp': '2026-06-29 10:00:00',
                'entry_creator_id': 7, 'nickname': 'Visitor',
                'content': 'Migrated comment',
                'created_at': '2026-06-29 10:01:00'}}
        }
        legacy_path.write_text(json.dumps(legacy_data), encoding='utf-8')

        with app.app_context():
            self.assertTrue(migrate_tinydb(legacy_path))
            self.assertFalse(migrate_tinydb(legacy_path))
            self.assertTrue(migrate_legacy_times_to_utc(legacy_path))
            self.assertFalse(migrate_legacy_times_to_utc(legacy_path))
            self.assertEqual(User.query.count(), 1)
            self.assertEqual(Post.query.count(), 1)
            self.assertEqual(Comment.query.count(), 1)
            user = db.session.get(User, 7)
            post = db.session.get(Post, 12)
            self.assertEqual(user.role, 'admin')
            self.assertEqual(user.profile.nickname, 'Legacy Author')
            self.assertEqual(post.content, 'Migrated post')
            self.assertEqual(post.legacy_likes, 2)
            self.assertEqual(post.board, 'public')
            self.assertEqual(post.created_at, datetime(2026, 6, 29, 2, 0))
            self.assertEqual(format_china_time(post.created_at),
                             '2026-06-29 10:00')
            self.assertEqual(db.session.get(Comment, 3).created_at,
                             datetime(2026, 6, 29, 2, 1))

        legacy_link = self.client.get(
            '/timestamp/2026-06-29 10:00:00', follow_redirects=False)
        self.assertEqual(legacy_link.status_code, 301)
        self.assertTrue(legacy_link.headers['Location'].endswith('/post/12'))

    def test_utc_times_render_as_china_time_and_local_day(self):
        account, _ = self.register('Timezone Author')
        with app.app_context():
            author = User.query.filter_by(username=account).one()
            post = Post(author_id=author.id, title='跨日测试', content='正文',
                        category='daily', status='published',
                        created_at=datetime(2026, 7, 1, 18, 30),
                        updated_at=datetime(2026, 7, 1, 18, 30))
            db.session.add(post)
            db.session.commit()
            post_id = post.id

        home = self.client.get('/').get_data(as_text=True)
        self.assertIn('2026-07-02 02:30', home)
        detail = self.client.get('/post/{}'.format(post_id)).get_data(
            as_text=True)
        self.assertIn('2026-07-02 02:30', detail)
        local_day = self.client.get('/day/2026-07-02').get_data(as_text=True)
        self.assertIn('跨日测试', local_day)
        self.assertEqual(self.client.get('/day/2026-07-01').status_code, 404)

    def test_structured_post_images_edit_cleanup_and_validation(self):
        alice_account, _ = self.register('Alice')
        self.login(alice_account)

        response = self.client.post('/post/new', data={
            'title': 'Image post',
            'content': 'A structured post with two images.',
            'tags': 'Vtuber, 音游',
            'images': [self.image_file('first.png', color='red'),
                       self.image_file('second.webp', 'WEBP', 'blue')],
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('article-detail-layout', page)
        self.assertIn('data-confirm-title="删除这篇博文？"', page)
        self.assertNotIn("return confirm('确定删除这条记录吗？')", page)
        self.assertIn('article-author-card', page)
        self.assertIn('article-info-card', page)
        self.assertIn('article-cover', page)
        self.assertIn('Image post', page)
        self.assertIn('custom.css?v=20260705-multi-image-picker', page)
        self.assertNotIn('发布板块', page)
        self.assertIn('post-gallery', page)
        # The first uploaded image is also the card/header cover, but must not
        # disappear from the article body gallery.
        self.assertEqual(page.count('alt="Image post 1"'), 1)
        self.assertEqual(page.count('alt="Image post 2"'), 1)
        home_page = self.client.get('/').get_data(as_text=True)
        self.assertIn('post-card-cover', home_page)
        self.assertIn('Image post', home_page)

        with app.app_context():
            post = Post.query.filter_by(title='Image post').one()
            post_id = post.id
            self.assertEqual(post.board, 'public')
            self.assertEqual(post.tags, ['Vtuber', '音游'])
            self.assertEqual(len(post.image_items), 2)
            self.assertEqual([item.sort_order for item in post.image_items],
                             [0, 1])
            first_item_id = post.image_items[0].id
            first_path = Path(self.upload_dir) / Path(
                post.image_items[0].media.file_path).name
            self.assertTrue(first_path.exists())

        editor_page = self.client.get('/post/{}/edit'.format(post_id)).get_data(
            as_text=True)
        self.assertIn('name="images"', editor_page)
        self.assertIn('multiple', editor_page)
        self.assertIn('js-post-images', editor_page)
        self.assertIn('data-max-images="9"', editor_page)
        self.assertIn('post-image-preview', editor_page)

        edit_response = self.client.post(
            '/post/{}/edit'.format(post_id), data={
                'title': 'Updated image post',
                'content': 'Updated content.',
                'tags': '演唱会',
                'remove_image_ids': str(first_item_id),
                'images': [self.image_file('replacement.jpg', 'JPEG', 'green')],
                'submit': '保存修改'
            }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(edit_response.status_code, 200)
        self.assertFalse(first_path.exists())

        with app.app_context():
            post = db.session.get(Post, post_id)
            self.assertEqual(post.title, 'Updated image post')
            self.assertEqual(post.board, 'public')
            self.assertEqual(len(post.image_items), 2)
            self.assertEqual([item.sort_order for item in post.image_items],
                             [0, 1])
            remaining_paths = [Path(self.upload_dir) /
                               Path(item.media.file_path).name
                               for item in post.image_items]
            self.assertTrue(all(path.exists() for path in remaining_paths))

        delete_response = self.client.post(
            '/post/{}/delete'.format(post_id),
            data={'submit': '删除记录'}, follow_redirects=True)
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(all(not path.exists() for path in remaining_paths))
        with app.app_context():
            self.assertEqual(db.session.get(Post, post_id).status, 'deleted')
            self.assertEqual(PostImage.query.count(), 0)
            self.assertEqual(Media.query.count(), 0)

        fake_response = self.client.post('/post/new', data={
            'title': 'Fake image', 'content': 'Must not be saved.',
            'tags': '',
            'images': [(BytesIO(b'<script>alert(1)</script>'), 'fake.png')],
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('文件内容不是有效图片',
                      fake_response.get_data(as_text=True))
        with app.app_context():
            self.assertIsNone(Post.query.filter_by(title='Fake image').first())

        too_many = [self.image_file('{}.png'.format(index))
                    for index in range(10)]
        count_response = self.client.post('/post/new', data={
            'title': 'Too many images', 'content': 'Reject ten images.',
            'tags': '', 'images': too_many,
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('最多上传 9 张图片',
                      count_response.get_data(as_text=True))
        with app.app_context():
            self.assertIsNone(Post.query.filter_by(
                title='Too many images').first())

        oversized_response = self.client.post('/post/new', data={
            'title': 'Oversized image', 'content': 'Reject oversized image.',
            'tags': '',
            'images': [(BytesIO(b'0' * (5 * 1024 * 1024 + 1)), 'large.png')],
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('单张图片不能超过 5MB',
                      oversized_response.get_data(as_text=True))
        with app.app_context():
            self.assertIsNone(Post.query.filter_by(
                title='Oversized image').first())

    def test_post_video_upload_replace_cleanup_and_validation(self):
        account, _ = self.register('Video User')
        self.login(account)

        response = self.client.post('/post/new', data={
            'title': 'Video post', 'content': 'A post with video.',
            'tags': '', 'video': self.video_file('first.mp4'),
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('class="post-video"', page)
        self.assertIn('type="video/mp4"', page)
        self.assertIn('class="video-playback-error"', page)
        self.assertIn('download', page)
        self.assertIn('class="article-cover-video-frame"', page)
        self.assertIn('article-cover-video video-cover-preview', page)
        self.assertEqual(page.count('<video'), 2)
        self.assertIn('preload="auto"', page)
        self.assertIn('function initializePostVideos()', page)
        self.assertIn("localStorage.setItem('musicPlaying', '0')", page)
        self.assertIn('video.currentTime = 0', page)
        self.assertIn('function freezeArticleCover()', page)
        self.assertIn("video.removeAttribute('src')", page)
        home = self.client.get('/').get_data(as_text=True)
        self.assertIn('class="video-cover-preview"', home)

        with app.app_context():
            post = Post.query.filter_by(title='Video post').one()
            post_id = post.id
            self.assertEqual(len(post.video_items), 1)
            self.assertEqual(len(post.stored_image_items), 0)
            video_item_id = post.video_items[0].id
            first_path = Path(self.upload_dir) / Path(
                post.video_items[0].media.file_path).name
            self.assertTrue(first_path.exists())

        replacement = self.client.post(
            '/post/{}/edit'.format(post_id), data={
                'title': 'Video post', 'content': 'Updated video.',
                'tags': '', 'remove_media_ids': str(video_item_id),
                'video': self.video_file('replacement.webm'),
                'submit': '保存修改'
            }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(replacement.status_code, 200)
        self.assertIn('type="video/webm"',
                      replacement.get_data(as_text=True))
        self.assertFalse(first_path.exists())

        with app.app_context():
            post = db.session.get(Post, post_id)
            self.assertEqual(len(post.video_items), 1)
            replacement_path = Path(self.upload_dir) / Path(
                post.video_items[0].media.file_path).name
            self.assertTrue(replacement_path.exists())

        invalid = self.client.post('/post/new', data={
            'title': 'Invalid video', 'content': 'Must not be saved.',
            'tags': '',
            'video': (BytesIO(b'not-a-real-video'), 'fake.mp4'),
            'submit': '发布博文'
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('不是有效的 MP4 视频',
                      invalid.get_data(as_text=True))
        with app.app_context():
            self.assertIsNone(Post.query.filter_by(
                title='Invalid video').first())

        converted_data = (b'\x00\x00\x00\x18ftypisom' +
                          b'\x00\x00\x00\x10avc1payload')
        with patch('app.uploads._transcode_hevc_mp4',
                   return_value=converted_data) as transcode:
            hevc = self.client.post('/post/new', data={
                'title': 'HEVC video', 'content': 'Browser-compatible.',
                'tags': '',
                'video': (BytesIO(b'\x00\x00\x00\x18ftypisom' +
                                  b'\x00\x00\x00\x10hvc1payload'),
                          'hevc.mp4'),
                'submit': '发布博文'
            }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(hevc.status_code, 200)
        transcode.assert_called_once()
        with app.app_context():
            converted_post = Post.query.filter_by(title='HEVC video').one()
            converted_path = Path(self.upload_dir) / Path(
                converted_post.video_items[0].media.file_path).name
            self.assertEqual(converted_path.read_bytes(), converted_data)

        deleted = self.client.post(
            '/post/{}/delete'.format(post_id),
            data={'submit': '删除记录'}, follow_redirects=True)
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(replacement_path.exists())

    def test_like_ajax_includes_valid_csrf_token(self):
        alice_account, _ = self.register('Alice')
        self.login(alice_account)
        self.publish('Like target', 'CSRF-protected like request')
        with app.app_context():
            post_id = Post.query.filter_by(title='Like target').one().id

        app.config['WTF_CSRF_ENABLED'] = True
        try:
            page = self.client.get('/')
            page_html = page.get_data(as_text=True)
            self.assertIn('js-post-like', page_html)
            self.assertIn('/api/like/{}'.format(post_id), page_html)
            match = re.search(
                r'name="csrf-token" content="([^"]+)"', page_html)
            self.assertIsNotNone(match)
            response = self.client.post(
                '/api/like/{}'.format(post_id),
                json={'action': 'like'},
                headers={'X-CSRFToken': match.group(1)})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()['liked'])

            entry_html = self.client.get(
                '/post/{}'.format(post_id)).get_data(as_text=True)
            self.assertIn('js-comment-form', entry_html)
            comment_response = self.client.post(
                '/api/post/{}/comments'.format(post_id),
                json={'content': 'AJAX comment without reload'},
                headers={'X-CSRFToken': match.group(1)})
            self.assertEqual(comment_response.status_code, 201)
            self.assertEqual(comment_response.get_json()['count'], 1)
            with app.app_context():
                self.assertIsNotNone(Comment.query.filter_by(
                    content='AJAX comment without reload').first())
        finally:
            app.config['WTF_CSRF_ENABLED'] = False

    def test_deactivate_preserves_content_and_sidebar_uses_avatar(self):
        account, _ = self.register('Alice')
        self.login(account)
        with app.app_context():
            alice = User.query.filter_by(username=account).one()
            self.assertEqual(alice.profile.avatar,
                             'images/default-avatar.jpg')
            user_id = alice.id

        avatar_response = self.client.post('/settings/profile', data={
            'nickname': 'Alice',
            'bio': 'profile',
            'avatar': self.image_file('avatar.png', color='pink')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(avatar_response.status_code, 200)
        with app.app_context():
            alice = db.session.get(User, user_id)
            avatar_path = alice.profile.avatar
            self.assertTrue(avatar_path.startswith('uploads/avatar/'))
        sidebar = self.client.get('/').get_data(as_text=True)
        self.assertIn('sidebar-profile-avatar', sidebar)
        self.assertIn(avatar_path, sidebar)

        self.publish('Preserved post', 'Content survives deactivation')
        with app.app_context():
            post_id = Post.query.filter_by(title='Preserved post').one().id
        self.client.post('/post/{}'.format(post_id), data={
            'content': 'Preserved comment', 'submit': '发表评论'
        }, follow_redirects=True)

        response = self.client.post('/settings/deactivate', data={
            'password': 'Secret1!', 'confirm': 'y',
            'submit': '确认注销账号'
        }, follow_redirects=True)
        self.assertIn('账号已注销', response.get_data(as_text=True))
        with app.app_context():
            alice = db.session.get(User, user_id)
            self.assertEqual(alice.status, 'deactivated')
            self.assertTrue(alice.username.startswith(
                '__deactivated_account_'))
            self.assertTrue(alice.profile.nickname.startswith(
                '__deactivated_user_'))
            self.assertIsNone(User.query.filter_by(username=account).first())
            self.assertIsNotNone(db.session.get(Post, post_id))
            self.assertIsNotNone(Comment.query.filter_by(
                content='Preserved comment').first())

        post_page = self.client.get(
            '/post/{}'.format(post_id)).get_data(as_text=True)
        self.assertIn('该用户已注销', post_page)
        self.assertEqual(self.client.get(
            '/user/{}'.format(user_id)).status_code, 404)

        unavailable_login = self.client.post('/admin/login', data={
            'account': account, 'password': 'Secret1!', 'submit': '登录'
        }, follow_redirects=True)
        self.assertIn('账号不存在', unavailable_login.get_data(as_text=True))

        reused_account, registration = self.register('Alice')
        self.assertEqual(reused_account, account)
        self.assertIn('您的账号是 {}'.format(account),
                      registration.get_data(as_text=True))
        post_after_reuse = self.client.get(
            '/post/{}'.format(post_id)).get_data(as_text=True)
        self.assertIn('该用户已注销', post_after_reuse)

    def test_stale_login_session_is_cleared(self):
        with self.client.session_transaction() as stale_session:
            stale_session['logged_in'] = True
            stale_session['user_id'] = 999999

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('sidebar-profile-avatar',
                         response.get_data(as_text=True))
        with self.client.session_transaction() as current_session:
            self.assertNotIn('logged_in', current_session)
            self.assertNotIn('user_id', current_session)

        self.assertEqual(self.client.get('/favicon.ico').status_code, 404)

    def test_music_context_priority_and_profile_player_is_global(self):
        alice_account, _ = self.register('Alice')
        bob_account, _ = self.register('Bob')
        charlie_account, _ = self.register('Charlie')
        with app.app_context():
            alice = User.query.filter_by(username=alice_account).one()
            bob = User.query.filter_by(username=bob_account).one()
            charlie = User.query.filter_by(username=charlie_account).one()
            db.session.add_all([
                UserMusicTrack(user_id=alice.id, title='Alice Song',
                               artist='A', audio_path='uploads/music/a.mp3',
                               source_type='upload'),
                UserMusicTrack(user_id=bob.id, title='Bob Cloud Song',
                               artist='B', audio_path='',
                               source_type='netease', source_id='123',
                               stream_url='https://example.test/123.mp3')
            ])
            alice_post = Post(author_id=alice.id, title='Alice context post',
                              content='context body', status='published')
            db.session.add(alice_post)
            db.session.commit()
            alice_id, bob_id, charlie_id = alice.id, bob.id, charlie.id
            alice_post_id = alice_post.id

        guest_profile = self.client.get(
            '/api/music/context?profile_user_id={}'.format(alice_id)).get_json()
        self.assertEqual(guest_profile['source'], 'user:{}'.format(alice_id))
        self.assertEqual(guest_profile['playlist'][0]['title'], 'Alice Song')
        self.assertEqual(self.client.get(
            '/api/music/context').get_json()['source'], 'default')

        self.login(bob_account)
        own_context = self.client.get('/api/music/context').get_json()
        self.assertEqual(own_context['source'], 'user:{}'.format(bob_id))
        self.assertEqual(own_context['playlist'][0]['source_type'], 'netease')
        self.assertEqual(own_context['playlist'][0]['audio'],
                         'https://example.test/123.mp3')
        other_profile = self.client.get(
            '/api/music/context?profile_user_id={}'.format(alice_id)).get_json()
        self.assertEqual(other_profile['source'], 'user:{}'.format(alice_id))
        no_music_profile = self.client.get(
            '/api/music/context?profile_user_id={}'.format(charlie_id)).get_json()
        self.assertEqual(no_music_profile['source'], 'default')
        self.assertEqual(no_music_profile['source_type'], 'default')

        profile_html = self.client.get(
            '/user/{}'.format(alice_id)).get_data(as_text=True)
        self.assertIn('data-profile-user-id="{}"'.format(alice_id), profile_html)
        own_profile_html = self.client.get(
            '/user/{}'.format(bob_id)).get_data(as_text=True)
        self.assertIn('data-profile-user-id=""', own_profile_html)
        post_html = self.client.get(
            '/post/{}'.format(alice_post_id)).get_data(as_text=True)
        self.assertIn('data-post-author-id="{}"'.format(alice_id), post_html)
        self.assertNotIn('data-post-id=', post_html)
        self.assertNotIn('new Audio()', profile_html)
        settings_html = self.client.get(
            '/settings/music').get_data(as_text=True)
        self.assertIn('add_netease', settings_html)
        self.assertIn('audio.loop = playlist.length === 1', profile_html)
        self.assertIn('effectiveDuration()', profile_html)
        self.assertNotIn('progress.dragging', profile_html)
        self.assertIn('trackKey', profile_html)
        self.assertIn('sameTrack = sameSource && keyIndex >= 0', profile_html)
        self.assertIn('rxMusicBrowseContext', profile_html)
        self.assertIn('musicBrowseProfileUserId !== postAuthorId', profile_html)
        self.assertIn('function navigatePjax', profile_html)
        self.assertIn('function refreshMusicContext', profile_html)
        self.assertIn("window.addEventListener('popstate'", profile_html)

    def test_netease_song_metadata_parsing_and_validation(self):
        payload = {'songs': [{
            'name': 'Cloud Song',
            'artists': [{'name': 'Cloud Artist'}],
            'album': {'picUrl': 'https://example.test/cover.jpg'}
        }]}
        with patch('app.netease._request_json', return_value=payload):
            song = query_netease_song('1809646618')
        self.assertEqual(song['title'], 'Cloud Song')
        self.assertEqual(song['artist'], 'Cloud Artist')
        self.assertEqual(song['cover'], 'https://example.test/cover.jpg')
        self.assertIn('1809646618.mp3', song['stream_url'])
        with self.assertRaises(NeteaseMusicError):
            query_netease_song('not-an-id')

        account, _ = self.register('Music User')
        self.login(account)
        user_views = importlib.import_module('app.user.views')
        with patch.object(user_views, 'query_netease_song', return_value=song):
            response = self.client.post('/settings/music', data={
                'action': 'add_netease', 'song_id': '1809646618'
            }, follow_redirects=True)
        self.assertIn('网易云歌曲已添加', response.get_data(as_text=True))
        with app.app_context():
            track = UserMusicTrack.query.filter_by(
                source_type='netease', source_id='1809646618').one()
            self.assertEqual(track.title, 'Cloud Song')
            self.assertEqual(track.audio_path, '')
            self.assertTrue(track.stream_url.endswith('1809646618.mp3'))

    def test_post_music_feature_is_removed(self):
        account, _ = self.register('Plain Post User')
        self.login(account)
        editor = self.client.get('/post/new').get_data(as_text=True)
        self.assertNotIn('博文背景音乐', editor)
        self.assertNotIn('music_track_id', editor)
        self.assertNotIn('netease_song_id', editor)
        with app.app_context():
            post_columns = {column['name'] for column in
                            inspect(db.engine).get_columns('posts')}
            self.assertNotIn('music_track_id', post_columns)
            # Existing databases may retain the retired column. SQLAlchemy
            # must continue to create and load posts while ignoring it.
            db.session.execute(text(
                'ALTER TABLE posts ADD COLUMN music_track_id INTEGER'))
            user = User.query.filter_by(username=account).one()
            db.session.add(Post(author_id=user.id, title='Legacy schema post',
                                content='body', status='published'))
            db.session.commit()
            self.assertIsNotNone(Post.query.filter_by(
                title='Legacy schema post').first())

    def test_ai_bot_is_idempotent_non_login_account(self):
        with app.app_context():
            first = ensure_ai_bot()
            first_id = first.id
            second = ensure_ai_bot()
            self.assertEqual(first_id, second.id)
            self.assertEqual(User.query.filter_by(
                username=AI_BOT_USERNAME).count(), 1)
            self.assertTrue(second.is_bot)
            self.assertEqual(second.profile.nickname, AI_BOT_USERNAME)

        human_account, _ = self.register('First Human')
        with app.app_context():
            self.assertEqual(User.query.filter_by(
                username=human_account).one().role, 'admin')
        login = self.client.post('/admin/login', data={
            'account': AI_BOT_USERNAME, 'password': 'Anything1!',
            'submit': '登录'}, follow_redirects=True)
        self.assertIn('账号不存在', login.get_data(as_text=True))
        with self.client.session_transaction() as login_session:
            self.assertNotIn('user_id', login_session)

    def test_ai_comment_rules_validation_and_generation_deduplication(self):
        service = AICommentService()
        short_post = Post(title='hi', content='...', category='daily')
        self.assertFalse(service.should_comment(short_post)['should_comment'])
        crisis_post = Post(title='求助', content='我有自残和轻生的想法',
                           category='daily')
        self.assertEqual(service.should_comment(crisis_post)['tone'], 'skip')
        work_post = Post(title='新作品', content='今天完成了摄影作品，分享一下',
                         category='creative')
        self.assertEqual(service.should_comment(work_post)['tone'], 'encouraging')
        self.assertEqual(service.validate_generated_comment(
            '<b>这个构图很有故事感，继续拍！</b>'),
            '这个构图很有故事感，继续拍！')
        self.assertIsNone(service.validate_generated_comment('SKIP_COMMENT'))
        with self.assertRaises(AICommentError):
            service.validate_generated_comment('x' * 81)
        with self.assertRaises(AICommentError) as incomplete:
            service.validate_generated_comment('正版特伯罗在此，盗版同学先交')
        self.assertEqual(incomplete.exception.code, 'incomplete_sentence')
        with patch.object(service, '_request', side_effect=[
                '正版特伯罗在此，盗版同学先交',
                '正版特伯罗在此，盗版同学先交一下版权费。']) as request_ai:
            with app.app_context():
                retried = service.generate_post_comment(work_post)
        self.assertEqual(retried, '正版特伯罗在此，盗版同学先交一下版权费。')
        self.assertEqual(request_ai.call_count, 2)

        account, _ = self.register('AI Post Author')
        self.login(account)
        self.publish('生活吐槽', '今天发生了一件很离谱但又好笑的事情。')
        with app.app_context():
            post = Post.query.filter_by(title='生活吐槽').one()
            post_id = post.id
        app.config['AI_COMMENT_PROBABILITY'] = 1.0
        try:
            with patch.object(AICommentService, 'generate_post_comment',
                              return_value='这剧情拐弯太快，生活编剧今天超常发挥。') as generate:
                response = self.client.post(
                    '/api/ai-comments/posts/{}/generate'.format(post_id),
                    json={})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.get_json()['status'], 'generated')
                duplicate = self.client.post(
                    '/api/ai-comments/posts/{}/generate'.format(post_id),
                    json={})
                self.assertEqual(duplicate.status_code, 200)
                self.assertEqual(generate.call_count, 1)
            with patch.object(AICommentService, 'generate_post_comment',
                              return_value='重新生成后，这次是一句完整评论。'):
                regenerated = self.client.post(
                    '/api/ai-comments/posts/{}/generate'.format(post_id),
                    json={'force': True})
                self.assertEqual(regenerated.status_code, 201)
                self.assertEqual(regenerated.get_json()['comment']['content'],
                                 '重新生成后，这次是一句完整评论。')
            with app.app_context():
                bot_comment = Comment.query.filter_by(
                    post_id=post_id, is_ai_generated=True,
                    status='deleted').one()
                visible_bot_comment = Comment.query.filter_by(
                    post_id=post_id, is_ai_generated=True,
                    status='visible').one()
                self.assertTrue(visible_bot_comment.author.is_bot)
                self.assertEqual(Comment.query.filter_by(
                    post_id=post_id, is_ai_generated=True).count(), 2)
                self.assertEqual(bot_comment.status, 'deleted')
                self.assertEqual(db.session.get(Post, post_id).ai_comment_status,
                                 'generated')
                self.assertEqual(AICommentAttempt.query.count(), 2)
            page = self.client.get('/post/{}'.format(post_id)).get_data(
                as_text=True)
            self.assertIn('AI机器人', page)
            self.assertIn('comment-teboluo-avatar.svg', page)
        finally:
            app.config['AI_COMMENT_PROBABILITY'] = 0.4

    def test_ai_failure_opt_out_and_client_forgery_are_safe(self):
        account, _ = self.register('Opt Out User')
        self.login(account)
        profile_update = self.client.post('/settings/profile', data={
            'nickname': 'Opt Out User', 'bio': 'No bot please'},
            follow_redirects=True)
        self.assertEqual(profile_update.status_code, 200)
        self.publish('不启用AI', '这是一篇正常且足够长的新帖子。')
        with app.app_context():
            post = Post.query.filter_by(title='不启用AI').one()
            self.assertFalse(post.allow_ai_comment)
            post_id = post.id
        disabled = self.client.post(
            '/api/ai-comments/posts/{}/generate'.format(post_id), json={})
        self.assertEqual(disabled.status_code, 403)

        forged = self.client.post('/api/post/{}/comments'.format(post_id),
                                  json={'content': '普通用户评论',
                                        'is_ai_generated': True,
                                        'author_id': 999999})
        self.assertEqual(forged.status_code, 201)
        with app.app_context():
            comment = Comment.query.filter_by(content='普通用户评论').one()
            self.assertFalse(comment.is_ai_generated)
            self.assertEqual(comment.author.username, account)

        with app.app_context():
            post = db.session.get(Post, post_id)
            post.allow_ai_comment = True
            db.session.commit()
        app.config.update(AI_COMMENT_PROBABILITY=1.0, AI_API_KEY='')
        failed = self.client.post(
            '/api/ai-comments/posts/{}/generate'.format(post_id), json={})
        self.assertEqual(failed.status_code, 503)
        with app.app_context():
            self.assertEqual(db.session.get(Post, post_id).ai_comment_status,
                             'failed')
            self.assertIsNotNone(Post.query.filter_by(title='不启用AI').first())
            self.assertIsNotNone(Comment.query.filter_by(
                content='普通用户评论').first())
        app.config.update(AI_COMMENT_PROBABILITY=0.4, AI_API_KEY='')


if __name__ == '__main__':
    unittest.main()
