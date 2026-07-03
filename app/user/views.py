from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from app.decorators import login_required
from app.auth.forms import DeactivateAccountForm
from app.accounts import release_account
from app.details import get_details
from app.extensions import db
from app.models import (Comment, GuestbookMessage, Post, PostImage, PostLike,
                        User, UserMusicTrack)
from app.netease import NeteaseMusicError, query_netease_song
from app.taxonomy import CATEGORIES, CATEGORY_META
import os

from app.uploads import (UploadValidationError, prepare_single_file,
                         single_file_absolute_path)
from . import user


@user.route('/user/<int:user_id>')
def view_user_profile(user_id):
    profile_user = db.session.get(User, user_id)
    if not profile_user or profile_user.status != 'active':
        abort(404)

    profile = profile_user.profile
    if not profile:
        abort(404)

    all_posts = db.session.scalars(
        select(Post).where(Post.author_id == user_id,
                           Post.status == 'published')
        .options(
            joinedload(Post.author).joinedload(User.profile),
            selectinload(Post.image_items).joinedload(PostImage.media)
        )
        .order_by(Post.created_at.desc(), Post.id.desc())
    ).all()

    post_count = len(all_posts)

    selected_category = request.args.get('category', '').strip()
    if selected_category not in CATEGORY_META:
        selected_category = ''
    selected_tag = request.args.get('tag', '').strip()

    category_counts = {slug: 0 for slug, _, _, _ in CATEGORIES}
    tag_counts = {}
    for post in all_posts:
        if post.category in category_counts:
            category_counts[post.category] += 1
        for tag in post.tags or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    posts = [post for post in all_posts
             if (not selected_category or post.category == selected_category)
             and (not selected_tag or selected_tag in (post.tags or []))]
    category_cards = []
    for slug, label, icon, description in CATEGORIES:
        category_cards.append({
            'slug': slug, 'label': label, 'icon': icon,
            'description': description, 'count': category_counts[slug]
        })
    popular_tags = sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:14]

    pinned_post = None
    latest_posts = posts
    if posts and not selected_category and not selected_tag:
        pinned_post = next((post for post in posts if post.is_pinned), None)
        if pinned_post:
            latest_posts = [post for post in posts if post.id != pinned_post.id]

    photo_items = []
    photo_posts = [post for post in all_posts if post.category == 'photo']
    for post in photo_posts:
        for image_path in post.display_images:
            photo_items.append({'post': post, 'path': image_path})
            if len(photo_items) == 6:
                break
        if len(photo_items) == 6:
            break

    like_count = db.session.scalar(
        select(func.count(PostLike.user_id))
        .join(Post, Post.id == PostLike.post_id)
        .where(Post.author_id == user_id)
    ) or 0

    comment_count = db.session.scalar(
        select(func.count(Comment.id))
        .join(Post, Post.id == Comment.post_id)
        .where(Post.author_id == user_id, Comment.status == 'visible')
    ) or 0

    guestbook_messages = db.session.scalars(
        select(GuestbookMessage).where(
            GuestbookMessage.profile_user_id == user_id,
            GuestbookMessage.status == 'visible'
        ).options(
            joinedload(GuestbookMessage.author).joinedload(User.profile)
        ).order_by(GuestbookMessage.created_at.desc())
    ).all()

    comment_counts = {}
    if all_posts:
        post_ids = [p.id for p in all_posts]
        rows = db.session.execute(
            select(Comment.post_id, func.count(Comment.id))
            .where(Comment.post_id.in_(post_ids), Comment.status == 'visible')
            .group_by(Comment.post_id)
        )
        comment_counts = {pid: cnt for pid, cnt in rows}

    like_rows = db.session.execute(
        select(PostLike.post_id, func.count(PostLike.user_id))
        .where(PostLike.post_id.in_([post.id for post in all_posts]))
        .group_by(PostLike.post_id)
    ) if all_posts else []
    stored_like_counts = {post_id: count for post_id, count in like_rows}
    like_counts = {
        post.id: post.legacy_likes + stored_like_counts.get(post.id, 0)
        for post in all_posts
    }
    liked_post_ids = set()
    if session.get('user_id') and all_posts:
        liked_post_ids = set(db.session.scalars(
            select(PostLike.post_id).where(
                PostLike.user_id == session['user_id'],
                PostLike.post_id.in_([post.id for post in all_posts])
            )
        ).all())

    is_owner = session.get('logged_in') and session['user_id'] == user_id
    is_admin = False
    if session.get('logged_in'):
        current_user = db.session.get(User, session['user_id'])
        is_admin = current_user and current_user.role == 'admin'

    return render_template('user_profile.html',
                           profile_user=profile_user,
                           profile=profile,
                           posts=posts,
                           latest_posts=latest_posts,
                           pinned_post=pinned_post,
                           category_cards=category_cards,
                           popular_tags=popular_tags,
                           photo_items=photo_items,
                           selected_category=selected_category,
                           selected_tag=selected_tag,
                           post_count=post_count,
                           like_count=like_count,
                           comment_count=comment_count,
                           guestbook_messages=guestbook_messages,
                           comment_counts=comment_counts,
                           like_counts=like_counts,
                           liked_post_ids=liked_post_ids,
                           is_owner=is_owner,
                           is_admin=is_admin,
                           music_profile_user_id='' if is_owner else user_id,
                           details=get_details())


@user.route('/settings/profile', methods=['GET', 'POST'])
@login_required
def settings_profile():
    current_user = db.session.get(User, session['user_id'])
    profile = current_user.profile

    if request.method == 'POST':
        nickname = request.form.get('nickname', '').strip()
        bio = request.form.get('bio', '').strip()

        if not nickname:
            flash('昵称不能为空。')
            return redirect(url_for('user.settings_profile'))

        profile.nickname = nickname
        profile.bio = bio[:500]
        current_user.allow_ai_comments = (
            request.form.get('allow_ai_comments') == 'on')

        try:
            avatar_file = request.files.get('avatar')
            if avatar_file and avatar_file.filename:
                prepared = prepare_single_file(avatar_file, 'image')
                if prepared:
                    from app.uploads import store_single_file
                    avatar_path = store_single_file(
                        current_user.id, prepared, 'avatar')
                    if profile.avatar:
                        old_path = single_file_absolute_path(profile.avatar)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    profile.avatar = avatar_path

            bg_file = request.files.get('background_image')
            if bg_file and bg_file.filename:
                prepared = prepare_single_file(bg_file, 'image')
                if prepared:
                    from app.uploads import store_single_file
                    bg_path = store_single_file(
                        current_user.id, prepared, 'background')
                    if profile.background_image:
                        old_path = single_file_absolute_path(
                            profile.background_image)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    profile.background_image = bg_path

            db.session.commit()
            flash('个人资料已更新。')
            return redirect(url_for('user.view_user_profile',
                                    user_id=current_user.id))
        except UploadValidationError as error:
            db.session.rollback()
            flash(str(error))
        except Exception:
            db.session.rollback()
            raise

    return render_template('settings_profile.html',
                           profile=profile,
                           deactivate_form=DeactivateAccountForm(),
                           details=get_details())


@user.route('/settings/deactivate', methods=['POST'])
@login_required
def deactivate_account():
    form = DeactivateAccountForm()
    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error)
        return redirect(url_for('user.settings_profile'))

    current_user = db.session.get(User, session['user_id'])
    released_account = current_user.username
    release_account(released_account)
    current_user.status = 'deactivated'
    current_user.username = '__deactivated_account_{}__'.format(current_user.id)
    current_user.profile.nickname = '__deactivated_user_{}__'.format(
        current_user.id)
    db.session.commit()
    session.clear()
    flash('账号已注销。历史帖子和评论将继续保留。')
    return redirect(url_for('main.browse_all_entries'))


@user.route('/settings/music', methods=['GET', 'POST'])
@login_required
def settings_music():
    current_user = db.session.get(User, session['user_id'])

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            title = request.form.get('title', '').strip()
            artist = request.form.get('artist', '').strip()
            audio_file = request.files.get('audio')
            cover_file = request.files.get('cover')

            if not title:
                flash('曲名不能为空。')
                return redirect(url_for('user.settings_music'))

            if not audio_file or not audio_file.filename:
                flash('请上传音频文件。')
                return redirect(url_for('user.settings_music'))

            try:
                from app.uploads import store_single_file
                audio_prepared = prepare_single_file(audio_file, 'audio')
                if not audio_prepared:
                    flash('音频文件格式不支持。')
                    return redirect(url_for('user.settings_music'))
                audio_path = store_single_file(
                    current_user.id, audio_prepared, 'music')

                cover_path = ''
                if cover_file and cover_file.filename:
                    cover_prepared = prepare_single_file(cover_file, 'image')
                    if cover_prepared:
                        cover_path = store_single_file(
                            current_user.id, cover_prepared, 'music_cover')

                track = UserMusicTrack(
                    user_id=current_user.id,
                    title=title,
                    artist=artist,
                    audio_path=audio_path,
                    cover_path=cover_path,
                    source_type='upload',
                    sort_order=0,
                    is_enabled=True
                )
                db.session.add(track)
                db.session.commit()
                flash('音乐已添加。')
            except UploadValidationError as error:
                db.session.rollback()
                flash(str(error))
            except Exception:
                db.session.rollback()
                raise

        elif action == 'add_netease':
            song_id = request.form.get('song_id', '').strip()
            try:
                song = query_netease_song(song_id)
                duplicate = UserMusicTrack.query.filter_by(
                    user_id=current_user.id, source_type='netease',
                    source_id=song['id']).first()
                if duplicate:
                    duplicate.title = song['title']
                    duplicate.artist = song['artist']
                    duplicate.stream_url = song['stream_url']
                    duplicate.cover_url = song['cover']
                    duplicate.duration_ms = song['duration_ms']
                    duplicate.is_enabled = True
                    db.session.commit()
                    flash('这首网易云歌曲的信息已更新。')
                else:
                    db.session.add(UserMusicTrack(
                        user_id=current_user.id,
                        title=song['title'], artist=song['artist'],
                        audio_path='', cover_path='',
                        source_type='netease', source_id=song['id'],
                        stream_url=song['stream_url'],
                        cover_url=song['cover'],
                        duration_ms=song['duration_ms'], is_enabled=True))
                    db.session.commit()
                    flash('网易云歌曲已添加。')
            except NeteaseMusicError as error:
                db.session.rollback()
                flash(str(error))

        elif action == 'delete':
            track_id = request.form.get('track_id', type=int)
            if track_id:
                track = db.session.get(UserMusicTrack, track_id)
                if track and track.user_id == current_user.id:
                    audio_path = (single_file_absolute_path(track.audio_path)
                                  if track.audio_path else None)
                    cover_path = single_file_absolute_path(
                        track.cover_path) if track.cover_path else None
                    if audio_path and os.path.exists(audio_path):
                        os.remove(audio_path)
                    if cover_path and os.path.exists(cover_path):
                        os.remove(cover_path)
                    db.session.delete(track)
                    db.session.commit()
                    flash('音乐已删除。')

        elif action == 'toggle':
            track_id = request.form.get('track_id', type=int)
            if track_id:
                track = db.session.get(UserMusicTrack, track_id)
                if track and track.user_id == current_user.id:
                    track.is_enabled = not track.is_enabled
                    db.session.commit()

        elif action == 'reorder':
            track_id = request.form.get('track_id', type=int)
            direction = request.form.get('direction')
            if track_id and direction in ('up', 'down'):
                track = db.session.get(UserMusicTrack, track_id)
                if track and track.user_id == current_user.id:
                    if direction == 'up':
                        track.sort_order -= 1
                    else:
                        track.sort_order += 1
                    db.session.commit()

        return redirect(url_for('user.settings_music'))

    tracks = db.session.scalars(
        select(UserMusicTrack).where(
            UserMusicTrack.user_id == current_user.id
        ).order_by(UserMusicTrack.sort_order, UserMusicTrack.id)
    ).all()

    return render_template('settings_music.html',
                           tracks=tracks,
                           details=get_details())


def _serialize_music_track(track):
    remote = track.source_type == 'netease'
    music_key = ('netease:{}'.format(track.source_id) if remote and
                 track.source_id else 'upload:{}'.format(track.id))
    return {
        'id': track.id,
        'key': music_key,
        'source_type': track.source_type,
        'source_id': track.source_id,
        'title': track.title,
        'artist': track.artist,
        'audio': (track.stream_url if remote else
                  url_for('static', filename=track.audio_path)),
        'cover': (track.cover_url if remote else
                  (url_for('static', filename=track.cover_path)
                   if track.cover_path else '')),
        'duration': (track.duration_ms or 0) / 1000
    }


def _enabled_music(user_id):
    if not user_id:
        return []
    owner = db.session.get(User, user_id)
    if not owner or owner.status != 'active':
        return []
    return db.session.scalars(
        select(UserMusicTrack).where(
            UserMusicTrack.user_id == user_id,
            UserMusicTrack.is_enabled == True
        ).order_by(UserMusicTrack.sort_order, UserMusicTrack.id)
    ).all()


@user.route('/api/music/context')
def api_music_context():
    profile_user_id = request.args.get('profile_user_id', type=int)
    tracks = _enabled_music(profile_user_id)
    source = 'user:{}'.format(profile_user_id) if tracks else ''

    # A profile owns its playback context. If that profile has no enabled
    # music, use the site default instead of leaking the visitor's playlist.
    if not profile_user_id and not tracks and session.get('logged_in'):
        current_user_id = session.get('user_id')
        tracks = _enabled_music(current_user_id)
        source = 'user:{}'.format(current_user_id) if tracks else ''

    if tracks:
        return jsonify({
            'source': source,
            'source_type': 'profile' if profile_user_id and
                           source == 'user:{}'.format(profile_user_id)
                           else 'self',
            'playlist': [_serialize_music_track(track) for track in tracks]
        })

    return _default_music_context()


def _default_music_context():
    return jsonify({
        'source': 'default',
        'source_type': 'default',
        'playlist': [{
            'id': 'default-bgm', 'key': 'default-bgm',
            'source_type': 'default',
            'source_id': '', 'title': '默认bgm', 'artist': '',
            'audio': url_for('static', filename='music/bgm.mp3'),
            'cover': url_for('static', filename='images/default-bgm-cover.png'),
            'duration': 0
        }]
    })


@user.route('/api/music/netease/<song_id>')
@login_required
def api_netease_music(song_id):
    try:
        return jsonify({'success': True,
                        'data': query_netease_song(song_id)})
    except NeteaseMusicError as error:
        return jsonify({'success': False, 'message': str(error)}), 400


@user.route('/guestbook/<int:user_id>/post', methods=['POST'])
@login_required
def post_guestbook(user_id):
    profile_user = db.session.get(User, user_id)
    if not profile_user:
        abort(404)

    content = request.form.get('content', '').strip()
    if not content:
        flash('留言内容不能为空。')
        return redirect(url_for('user.view_user_profile', user_id=user_id))

    if len(content) > 500:
        flash('留言内容不能超过500字。')
        return redirect(url_for('user.view_user_profile', user_id=user_id))

    message = GuestbookMessage(
        profile_user_id=user_id,
        author_id=session['user_id'],
        content=content
    )
    db.session.add(message)
    db.session.commit()
    flash('留言成功。')
    return redirect(url_for('user.view_user_profile', user_id=user_id))


@user.route('/guestbook/<int:message_id>/delete', methods=['POST'])
@login_required
def delete_guestbook(message_id):
    message = db.session.get(GuestbookMessage, message_id)
    if not message:
        abort(404)

    current_user = db.session.get(User, session['user_id'])
    is_owner = message.profile_user_id == session['user_id']
    is_admin = current_user and current_user.role == 'admin'

    if not is_owner and not is_admin:
        abort(403)

    message.status = 'deleted'
    db.session.commit()
    flash('留言已删除。')
    return redirect(url_for('user.view_user_profile',
                            user_id=message.profile_user_id))
