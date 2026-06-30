from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, select

from app.decorators import login_required
from app.auth.forms import DeactivateAccountForm
from app.accounts import release_account
from app.details import get_details
from app.extensions import db
from app.models import (Comment, GuestbookMessage, Post, PostLike,
                        User, UserMusicTrack)
import os
from pathlib import Path

from app.uploads import (UploadValidationError, cleanup_paths,
                         prepare_single_file, single_file_absolute_path)
from . import user


@user.route('/user/<int:user_id>')
def view_user_profile(user_id):
    profile_user = db.session.get(User, user_id)
    if not profile_user or profile_user.status != 'active':
        abort(404)

    profile = profile_user.profile
    if not profile:
        abort(404)

    posts = db.session.scalars(
        select(Post).where(Post.author_id == user_id,
                           Post.status == 'published')
        .order_by(Post.created_at.desc(), Post.id.desc())
    ).all()

    post_count = len(posts)

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

    music_tracks = db.session.scalars(
        select(UserMusicTrack).where(
            UserMusicTrack.user_id == user_id,
            UserMusicTrack.is_enabled == True
        ).order_by(UserMusicTrack.sort_order, UserMusicTrack.id)
    ).all()

    guestbook_messages = db.session.scalars(
        select(GuestbookMessage).where(
            GuestbookMessage.profile_user_id == user_id,
            GuestbookMessage.status == 'visible'
        ).order_by(GuestbookMessage.created_at.desc())
    ).all()

    comment_counts = {}
    if posts:
        post_ids = [p.id for p in posts]
        rows = db.session.execute(
            select(Comment.post_id, func.count(Comment.id))
            .where(Comment.post_id.in_(post_ids), Comment.status == 'visible')
            .group_by(Comment.post_id)
        )
        comment_counts = {pid: cnt for pid, cnt in rows}

    is_owner = session.get('logged_in') and session['user_id'] == user_id
    is_admin = False
    if session.get('logged_in'):
        current_user = db.session.get(User, session['user_id'])
        is_admin = current_user and current_user.role == 'admin'

    return render_template('user_profile.html',
                           profile_user=profile_user,
                           profile=profile,
                           posts=posts,
                           post_count=post_count,
                           like_count=like_count,
                           comment_count=comment_count,
                           music_tracks=music_tracks,
                           guestbook_messages=guestbook_messages,
                           comment_counts=comment_counts,
                           is_owner=is_owner,
                           is_admin=is_admin,
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

        elif action == 'delete':
            track_id = request.form.get('track_id', type=int)
            if track_id:
                track = db.session.get(UserMusicTrack, track_id)
                if track and track.user_id == current_user.id:
                    audio_path = single_file_absolute_path(track.audio_path)
                    cover_path = single_file_absolute_path(
                        track.cover_path) if track.cover_path else None
                    if os.path.exists(audio_path):
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


@user.route('/api/user/<int:user_id>/music')
def api_user_music(user_id):
    tracks = db.session.scalars(
        select(UserMusicTrack).where(
            UserMusicTrack.user_id == user_id,
            UserMusicTrack.is_enabled == True
        ).order_by(UserMusicTrack.sort_order, UserMusicTrack.id)
    ).all()

    result = []
    for t in tracks:
        result.append({
            'id': t.id,
            'title': t.title,
            'artist': t.artist,
            'audio': url_for('static', filename=t.audio_path),
            'cover': url_for('static', filename=t.cover_path) if t.cover_path else ''
        })

    return jsonify(result)


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
