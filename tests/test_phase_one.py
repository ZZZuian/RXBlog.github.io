import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path


_temp_dir = tempfile.TemporaryDirectory()
_root = Path(_temp_dir.name)
os.environ['CHRONOFLASK_DATABASE_URI'] = 'sqlite:///{}'.format(
    (_root / 'test-community.db').as_posix())
os.environ['CHRONOFLASK_DB_PATH'] = str(_root / 'missing-legacy.json')

from sqlalchemy import inspect, select
from PIL import Image

from app import app
from app.extensions import db
from app.migration import migrate_tinydb
from app.models import (Comment, Media, MigrationState, Post, PostImage,
                        PostLike, Profile, SiteSetting, User)


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
            alice_day = alice_post.created_at.strftime('%Y-%m-%d')
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
        self.assertIn('Image post', page)
        self.assertNotIn('发布板块', page)
        self.assertIn('post-gallery', page)
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


if __name__ == '__main__':
    unittest.main()
