from datetime import datetime, timedelta, timezone
import logging
import random

from flask import (abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload, selectinload

from app.decorators import login_required
from app.details import get_details
from app.extensions import db
from app.models import (AICommentAttempt, Comment, Post, PostImage, PostLike,
                        SiteSetting, User, utcnow)
from app.migration import AI_BOT_USERNAME, ensure_ai_bot
from app.services.ai_comment_service import AICommentError, AICommentService
from app.parse import parse_input
from app.taxonomy import category_meta, normalize_tags
from app.time_utils import format_china_time
from app.uploads import (UploadValidationError, cleanup_paths,
                         media_absolute_path, prepare_images, prepare_videos,
                         store_images, store_videos)
from . import main
from .forms import (CommentForm, DeleteEntryForm, PinPostForm, PostForm,
                    RawEntryForm)


PER_PAGE = 10
LOGGER = logging.getLogger(__name__)


@main.app_context_processor
def inject_liked_post_ids():
    """Keep post-card includes safe outside the feed views."""
    return {'liked_post_ids': set()}


@main.app_context_processor
def inject_post_taxonomy():
    return {'category_meta': category_meta}


def _parse_tags(raw_tags):
    tags = []
    for value in (raw_tags or '').replace('，', ',').split(','):
        tag = value.strip()
        if tag and tag not in tags:
            tags.append(tag)
    return normalize_tags(tags)


def _add_image_error(form, message):
    errors = list(form.images.errors)
    errors.append(message)
    form.images.errors = errors


def _add_video_error(form, message):
    errors = list(form.video.errors)
    errors.append(message)
    form.video.errors = errors


def _is_admin(user_id=None):
    user = db.session.get(User, user_id or session.get('user_id'))
    return bool(user and user.role == 'admin' and user.status == 'active')


def _can_manage_post(post):
    return post.author_id == session.get('user_id') or _is_admin()


def _ai_comments_enabled():
    setting = db.session.get(SiteSetting, 'ai_comment_enabled')
    if setting:
        return setting.value.lower() in ('1', 'true', 'yes', 'on')
    return bool(current_app.config.get('AI_COMMENT_ENABLED', True))


def _ai_comment_probability():
    setting = db.session.get(SiteSetting, 'ai_comment_probability')
    raw_value = (setting.value if setting else
                 current_app.config.get('AI_COMMENT_PROBABILITY', 0.4))
    try:
        return max(0.0, min(1.0, float(raw_value)))
    except (TypeError, ValueError):
        return 0.4


def _serialize_comment(comment):
    user = comment.author
    avatar = (user.profile.avatar if user and user.profile and
              user.profile.avatar else 'images/default-avatar.jpg')
    return {
        'id': comment.id,
        'content': comment.content,
        'nickname': comment.nickname,
        'profile_url': (url_for('user.view_user_profile', user_id=user.id)
                        if user else ''),
        'avatar_url': url_for('static', filename=avatar),
        'created_at': format_china_time(comment.created_at),
        'is_bot': bool(user and user.is_bot),
        'is_ai_generated': bool(comment.is_ai_generated),
    }


def _set_profile_pin(post, should_pin):
    """Keep at most one published profile pin for each author."""
    post.is_pinned = bool(should_pin)
    if not should_pin:
        return
    statement = Post.query.filter(
        Post.author_id == post.author_id,
        Post.is_pinned.is_(True)
    )
    if post.id is not None:
        statement = statement.filter(Post.id != post.id)
    statement.update({Post.is_pinned: False}, synchronize_session=False)


def _published_post(post_id):
    return db.session.scalar(select(Post).where(
        Post.id == post_id, Post.status == 'published')
        .options(
            joinedload(Post.author).joinedload(User.profile),
            selectinload(Post.image_items).joinedload(PostImage.media)
        ))


def _post_by_timestamp(timestamp):
    post = db.session.scalar(
        select(Post).where(
            func.strftime('%Y-%m-%d %H:%M:%S', Post.created_at) == timestamp,
            Post.status == 'published'
        ).order_by(Post.id)
    )
    if post:
        return post
    # TinyDB links used China-local naive timestamps before UTC normalization.
    legacy_utc = datetime.strptime(
        timestamp, '%Y-%m-%d %H:%M:%S') - timedelta(hours=8)
    return db.session.scalar(
        select(Post).where(
            Post.created_at == legacy_utc,
            Post.status == 'published'
        ).order_by(Post.id)
    )


def _comment_counts(post_ids):
    if not post_ids:
        return {}
    rows = db.session.execute(
        select(Comment.post_id, func.count(Comment.id))
        .where(Comment.post_id.in_(post_ids), Comment.status == 'visible')
        .group_by(Comment.post_id)
    )
    return {post_id: count for post_id, count in rows}


def _like_counts(posts):
    """Return like totals in one query instead of loading each like collection."""
    if not posts:
        return {}
    post_ids = [post.id for post in posts]
    rows = db.session.execute(
        select(PostLike.post_id, func.count(PostLike.user_id))
        .where(PostLike.post_id.in_(post_ids))
        .group_by(PostLike.post_id)
    )
    stored_counts = {post_id: count for post_id, count in rows}
    return {post.id: post.legacy_likes + stored_counts.get(post.id, 0)
            for post in posts}


def _liked_post_ids(post_ids):
    user_id = session.get('user_id')
    if not user_id or not post_ids:
        return set()
    return set(db.session.scalars(
        select(PostLike.post_id).where(
            PostLike.user_id == user_id,
            PostLike.post_id.in_(post_ids)
        )
    ).all())


def _post_card_options():
    """Relationships used by every post card, loaded in bounded queries."""
    return (
        joinedload(Post.author).joinedload(User.profile),
        selectinload(Post.image_items).joinedload(PostImage.media),
    )


def _render_feed(template, page, **context):
    posts = page.items
    post_ids = [post.id for post in posts]
    context.update(
        entries_for_page=posts,
        comment_counts=_comment_counts(post_ids),
        like_counts=_like_counts(posts),
        liked_post_ids=_liked_post_ids(post_ids),
        next_page=page.next_num if page.has_next else None,
        prev_page=page.prev_num if page.has_prev else None,
        details=get_details()
    )
    return render_template(template, **context)


@main.route('/', methods=['GET', 'POST'])
def browse_all_entries():
    form = RawEntryForm()
    if request.method == 'POST':
        if not session.get('logged_in'):
            flash('请先登录后再发布内容。')
            return redirect(url_for('auth.login', next=url_for('main.browse_all_entries')))
        if form.validate_on_submit():
            return parse_input(form.raw_entry.data, datetime.now(timezone.utc))
    page_number = request.args.get('page', 1, type=int)
    statement = (select(Post).where(Post.status == 'published')
                 .options(*_post_card_options())
                 .order_by(Post.created_at.desc(), Post.id.desc()))
    page = db.paginate(statement, page=page_number, per_page=PER_PAGE,
                       error_out=False)
    return _render_feed('home.html', page, form=form)


@main.route('/post/new', methods=['GET', 'POST'])
@login_required
def create_post():
    form = PostForm()
    if form.validate_on_submit():
        created_paths = []
        upload_field = 'images'
        try:
            prepared_images = prepare_images(form.images.data or [])
            upload_field = 'video'
            prepared_videos = prepare_videos([form.video.data])
            author = db.session.get(User, session['user_id'])
            post = Post(author_id=session['user_id'], title=form.title.data.strip(),
                        content=form.content.data.strip(), board='public',
                        category=form.category.data,
                        tags=_parse_tags(form.tags.data), status='published',
                        allow_ai_comment=author.allow_ai_comments)
            db.session.add(post)
            db.session.flush()
            created_paths = store_images(
                post, session['user_id'], prepared_images)
            created_paths.extend(store_videos(
                post, session['user_id'], prepared_videos))
            db.session.commit()
            flash('博文发布成功。')
            return redirect(url_for('main.view_post', post_id=post.id))
        except UploadValidationError as error:
            db.session.rollback()
            cleanup_paths(created_paths)
            if upload_field == 'video':
                _add_video_error(form, str(error))
            else:
                _add_image_error(form, str(error))
        except Exception:
            db.session.rollback()
            cleanup_paths(created_paths)
            raise
    return render_template('post.html', form=form, details=get_details(),
                           page_title='发布博文', post=None)


@main.route('/page/<int:page>', methods=['GET', 'POST'])
def view_entries_for_page(page):
    if page <= 1:
        return redirect(url_for('main.browse_all_entries'))
    form = RawEntryForm()
    if request.method == 'POST':
        if not session.get('logged_in'):
            return redirect(url_for('auth.login', next=request.url))
        if form.validate_on_submit():
            return parse_input(form.raw_entry.data, datetime.now(timezone.utc))
    statement = (select(Post).where(Post.status == 'published')
                 .options(*_post_card_options())
                 .order_by(Post.created_at.desc(), Post.id.desc()))
    pagination = db.paginate(statement, page=page, per_page=PER_PAGE,
                             error_out=False)
    if not pagination.items:
        abort(404)
    return _render_feed('page.html', pagination, form=form, page=page)


@main.route('/day/<day>', methods=['GET'])
def view_entries_for_day(day):
    posts = db.session.scalars(
        select(Post).where(Post.status == 'published',
                           func.date(func.datetime(
                               Post.created_at, '+8 hours')) == day)
        .options(*_post_card_options())
        .order_by(Post.created_at.asc(), Post.id.asc())
    ).all()
    if not posts:
        abort(404)
    return render_template('day.html', entries_for_day=posts, day=day,
                           details=get_details(),
                           comment_counts=_comment_counts([p.id for p in posts]),
                           like_counts=_like_counts(posts),
                           liked_post_ids=_liked_post_ids([p.id for p in posts]))


@main.route('/post/<int:post_id>', methods=['GET', 'POST'])
def view_post(post_id):
    post = _published_post(post_id)
    if not post:
        abort(404)
    comment_form = CommentForm()
    if comment_form.validate_on_submit():
        if not session.get('logged_in'):
            flash('请先登录后再评论。')
            return redirect(url_for('auth.login', next=request.url))
        comment = Comment(post_id=post.id, author_id=session['user_id'],
                          content=comment_form.content.data)
        db.session.add(comment)
        db.session.commit()
        flash('评论发表成功。')
        return redirect(url_for('main.view_post', post_id=post.id))
    comments = db.session.scalars(
        select(Comment).where(Comment.post_id == post.id,
                              Comment.status == 'visible')
        .options(joinedload(Comment.author).joinedload(User.profile))
        .order_by(Comment.created_at.asc(), Comment.id.asc())
    ).all()
    liked = bool(session.get('user_id') and db.session.get(
        PostLike, (session['user_id'], post.id)))

    author_post_count = db.session.scalar(
        select(func.count(Post.id)).where(
            Post.author_id == post.author_id,
            Post.status == 'published')) or 0
    author_like_count = db.session.scalar(
        select(func.count(PostLike.user_id)).join(
            Post, Post.id == PostLike.post_id).where(
            Post.author_id == post.author_id,
            Post.status == 'published')) or 0
    author_comment_count = db.session.scalar(
        select(func.count(Comment.id)).join(
            Post, Post.id == Comment.post_id).where(
            Post.author_id == post.author_id,
            Post.status == 'published',
            Comment.status == 'visible')) or 0
    reading_minutes = max(1, (len(post.content.strip()) + 399) // 400)
    
    is_admin = False
    if session.get('logged_in'):
        user = db.session.get(User, session['user_id'])
        is_admin = user and user.role == 'admin'
    
    return render_template('entry.html', entry=post, post=post,
                           details=get_details(), comments=comments,
                           comment_form=comment_form,
                           delete_form=DeleteEntryForm(),
                           pin_form=PinPostForm(), liked=liked,
                           post_like_count=_like_counts([post])[post.id],
                           is_admin=is_admin,
                           author_post_count=author_post_count,
                           author_like_count=author_like_count,
                           author_comment_count=author_comment_count,
                           reading_minutes=reading_minutes,
                           page_post_author_id=post.author_id,
                           ai_comments_enabled=_ai_comments_enabled())


@main.route('/timestamp/<timestamp>')
def view_single_entry(timestamp):
    """Redirect legacy timestamp links to the stable post ID route."""
    try:
        datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        abort(404)
    post = _post_by_timestamp(timestamp)
    if not post:
        abort(404)
    return redirect(url_for('main.view_post', post_id=post.id), code=301)


@main.route('/post/<int:post_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    post = _published_post(post_id)
    if not post:
        abort(404)
    if not _can_manage_post(post):
        abort(403)
    form = PostForm()
    if form.validate_on_submit():
        selected_values = (request.form.getlist('remove_media_ids') +
                           request.form.getlist('remove_image_ids'))
        selected_ids = {int(value) for value in selected_values
                        if value.isdigit()}
        remove_items = [item for item in post.image_items
                        if item.id in selected_ids]
        removed_image_count = sum(
            1 for item in remove_items
            if item.media and item.media.media_type == 'image')
        removed_video_count = sum(
            1 for item in remove_items
            if item.media and item.media.media_type == 'video')
        existing_image_count = (len(post.stored_image_items) +
                                len(post.images or []) - removed_image_count)
        existing_video_count = len(post.video_items) - removed_video_count
        created_paths = []
        removed_paths = [media_absolute_path(item.media)
                         for item in remove_items]
        upload_field = 'images'
        try:
            prepared_images = prepare_images(
                form.images.data or [], existing_image_count)
            upload_field = 'video'
            prepared_videos = prepare_videos(
                [form.video.data], existing_video_count)
            post.title = form.title.data.strip()
            post.content = form.content.data.strip()
            post.board = 'public'
            post.category = form.category.data
            post.tags = _parse_tags(form.tags.data)
            post.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            for item in remove_items:
                media = item.media
                post.image_items.remove(item)
                db.session.delete(media)
            for index, item in enumerate(post.image_items):
                item.sort_order = index
            created_paths = store_images(
                post, session['user_id'], prepared_images)
            created_paths.extend(store_videos(
                post, session['user_id'], prepared_videos))
            db.session.commit()
            cleanup_paths(removed_paths)
            flash('博文已更新。')
            return redirect(url_for('main.view_post', post_id=post.id))
        except UploadValidationError as error:
            db.session.rollback()
            cleanup_paths(created_paths)
            if upload_field == 'video':
                _add_video_error(form, str(error))
            else:
                _add_image_error(form, str(error))
        except Exception:
            db.session.rollback()
            cleanup_paths(created_paths)
            raise
    if request.method == 'GET':
        form.title.data = post.title or ''
        form.content.data = post.content
        form.category.data = post.category
        form.tags.data = ', '.join(post.tags or [])
        form.submit.label.text = '保存修改'
    return render_template('post.html', form=form, post=post,
                           page_title='编辑博文', details=get_details())


@main.route('/timestamp/<timestamp>/edit')
@login_required
def edit_entry(timestamp):
    try:
        datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        abort(404)
    post = _post_by_timestamp(timestamp)
    if not post:
        abort(404)
    return redirect(url_for('main.edit_post', post_id=post.id), code=301)


@main.route('/post/<int:post_id>/pin', methods=['POST'])
@login_required
def toggle_post_pin(post_id):
    post = _published_post(post_id)
    if not post:
        abort(404)
    if not _can_manage_post(post):
        abort(403)
    form = PinPostForm()
    if not form.validate_on_submit():
        abort(400)
    was_pinned = post.is_pinned
    _set_profile_pin(post, not was_pinned)
    db.session.commit()
    flash('已取消主页置顶。' if was_pinned else '已置顶到个人主页。')
    return redirect(url_for('main.view_post', post_id=post.id))


@main.route('/post/<int:post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    post = _published_post(post_id)
    if not post:
        abort(404)
    if not _can_manage_post(post):
        abort(403)
    form = DeleteEntryForm()
    if not form.validate_on_submit():
        abort(400)
    removed_paths = [media_absolute_path(item.media)
                     for item in post.image_items]
    for item in list(post.image_items):
        media = item.media
        post.image_items.remove(item)
        db.session.delete(media)
    post.status = 'deleted'
    post.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    cleanup_paths(removed_paths)
    flash('记录已删除。')
    return redirect(url_for('main.browse_all_entries'))


@main.route('/search')
def search_posts():
    query = request.args.get('q', '').strip()
    if not query:
        return redirect(url_for('main.browse_all_entries'))
    
    page_number = request.args.get('page', 1, type=int)
    
    search_pattern = f'%{query}%'
    statement = (
        select(Post)
        .options(*_post_card_options())
        .where(
            Post.status == 'published',
            db.or_(
                Post.title.ilike(search_pattern),
                Post.content.ilike(search_pattern),
                Post.tags.cast(db.String).ilike(search_pattern)
            )
        )
        .order_by(Post.created_at.desc(), Post.id.desc())
    )
    
    page = db.paginate(statement, page=page_number, per_page=PER_PAGE,
                       error_out=False)
    return _render_feed('search.html', page, query=query,
                        details=get_details())


@main.route('/tags')
def view_all_tags():
    posts = db.session.scalars(select(Post).where(
        Post.status == 'published')).all()
    tag_counts = {}
    for post in posts:
        for tag in (post.tags or []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    all_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    return render_template('tags.html', all_tags=all_tags, details=get_details())


@main.route('/days')
def view_all_days():
    days = db.session.scalars(
        select(func.date(func.datetime(Post.created_at, '+8 hours')))
        .where(Post.status == 'published').distinct()
        .order_by(func.date(func.datetime(
            Post.created_at, '+8 hours')).desc())
    ).all()
    return render_template('days.html', all_days=days, details=get_details())


@main.route('/tags/<tag>')
def view_entries_for_tag(tag):
    posts = db.session.scalars(
        select(Post).where(Post.status == 'published')
        .options(*_post_card_options())
        .order_by(Post.created_at.desc(), Post.id.desc())
    ).all()
    posts = [post for post in posts if tag in (post.tags or [])]
    if not posts:
        abort(404)
    return render_template('tag.html', entries_for_tag=posts, tag=tag,
                           details=get_details(),
                           comment_counts=_comment_counts([p.id for p in posts]),
                           like_counts=_like_counts(posts),
                           liked_post_ids=_liked_post_ids([p.id for p in posts]))


@main.route('/api/like/<int:post_id>', methods=['POST'])
@login_required
def like_post(post_id):
    post = _published_post(post_id)
    if not post:
        return jsonify({'error': 'not found'}), 404
    key = (session['user_id'], post.id)
    like = db.session.get(PostLike, key)
    action = request.get_json(silent=True) or {}
    if action.get('action') == 'unlike':
        if like:
            db.session.delete(like)
        liked = False
    else:
        if not like:
            db.session.add(PostLike(user_id=session['user_id'], post_id=post.id))
        liked = True
    db.session.commit()
    like_count = post.legacy_likes + (db.session.scalar(
        select(func.count(PostLike.user_id)).where(
            PostLike.post_id == post.id)) or 0)
    return jsonify({'likes': like_count, 'liked': liked})


@main.route('/api/like/<timestamp>', methods=['POST'])
@login_required
def like_entry(timestamp):
    try:
        datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return jsonify({'error': 'not found'}), 404
    post = _post_by_timestamp(timestamp)
    if not post:
        return jsonify({'error': 'not found'}), 404
    return like_post(post.id)


@main.route('/api/post/<int:post_id>/comments', methods=['POST'])
@login_required
def api_add_comment(post_id):
    post = _published_post(post_id)
    if not post:
        return jsonify({'error': '博文不存在。'}), 404
    payload = request.get_json(silent=True) or {}
    content = str(payload.get('content') or '').strip()
    if not content:
        return jsonify({'error': '评论内容不能为空。'}), 400
    if len(content) > 500:
        return jsonify({'error': '评论不能超过 500 个字符。'}), 400
    comment = Comment(post_id=post.id, author_id=session['user_id'],
                      content=content)
    db.session.add(comment)
    db.session.commit()
    comment_count = db.session.scalar(
        select(func.count(Comment.id)).where(
            Comment.post_id == post.id, Comment.status == 'visible')) or 0
    return jsonify({
        'comment': _serialize_comment(comment),
        'count': comment_count
    }), 201


@main.route('/api/ai-comments/posts/<int:post_id>/generate', methods=['POST'])
@login_required
def generate_ai_comment(post_id):
    post = db.session.get(Post, post_id)
    requester = db.session.get(User, session['user_id'])
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get('force')) and requester.role == 'admin'
    if not post or post.status != 'published':
        return jsonify({'error': '博文不存在。'}), 404
    if post.author_id != requester.id and requester.role != 'admin':
        return jsonify({'error': '只能触发自己博文的AI评论。'}), 403
    if not _ai_comments_enabled():
        return jsonify({'error': 'AI评论已关闭。', 'status': 'disabled'}), 403
    if not post.allow_ai_comment and not force:
        return jsonify({'error': '作者已关闭AI评论。', 'status': 'disabled'}), 403
    existing = Comment.query.filter_by(
        post_id=post.id, is_ai_generated=True, status='visible').first()
    previous_comment_id = existing.id if existing and force else None
    if existing and not force:
        return jsonify({'status': 'generated',
                        'comment': _serialize_comment(existing),
                        'count': _comment_counts([post.id]).get(post.id, 0)})

    now = utcnow()
    if post.ai_comment_status == 'pending':
        fresh_after = now - timedelta(minutes=2)
        if (post.ai_comment_updated_at and
                post.ai_comment_updated_at >= fresh_after):
            return jsonify({'status': 'pending'}), 409
        post.ai_comment_status = 'failed'
        post.ai_comment_error = 'stale_pending'
        db.session.commit()

    max_attempts = current_app.config.get('AI_COMMENT_MAX_ATTEMPTS', 2)
    if post.ai_comment_attempts >= max_attempts and not force:
        return jsonify({'error': '该博文的AI评论重试次数已用完。',
                        'status': post.ai_comment_status}), 429

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_count = db.session.scalar(select(func.count(AICommentAttempt.id)).where(
        AICommentAttempt.requested_by == requester.id,
        AICommentAttempt.created_at >= day_start)) or 0
    if daily_count >= current_app.config.get('AI_COMMENT_USER_DAILY_LIMIT', 10):
        return jsonify({'error': '今日AI评论触发次数已达上限。'}), 429
    minute_count = db.session.scalar(select(func.count(AICommentAttempt.id)).where(
        AICommentAttempt.created_at >= now - timedelta(minutes=1))) or 0
    if minute_count >= current_app.config.get('AI_COMMENT_GLOBAL_MINUTE_LIMIT', 20):
        return jsonify({'error': 'AI评论请求较多，请稍后再试。'}), 429

    allowed_statuses = ['none']
    if force:
        allowed_statuses.extend(['failed', 'skipped', 'generated'])
    claimed = db.session.execute(update(Post).where(
        Post.id == post.id,
        Post.ai_comment_status.in_(allowed_statuses)).values(
            ai_comment_status='pending',
            ai_comment_attempts=Post.ai_comment_attempts + 1,
            ai_comment_error='', ai_comment_updated_at=now),
        execution_options={'synchronize_session': False}).rowcount
    db.session.commit()
    if not claimed:
        db.session.refresh(post)
        return jsonify({'status': post.ai_comment_status}), 409

    probability = _ai_comment_probability()
    if not force and random.random() > probability:
        post = db.session.get(Post, post.id)
        post.ai_comment_status = 'skipped'
        post.ai_comment_error = 'probability'
        post.ai_comment_updated_at = utcnow()
        db.session.commit()
        return jsonify({'status': 'skipped'})

    service = AICommentService()
    decision = service.should_comment(post)
    if not decision['should_comment']:
        post.ai_comment_status = ('generated' if previous_comment_id
                                  else 'skipped')
        post.ai_comment_error = ('regenerate_content_rule'
                                 if previous_comment_id else 'content_rule')
        post.ai_comment_updated_at = utcnow()
        db.session.commit()
        return jsonify({'status': post.ai_comment_status,
                        'retained_previous': bool(previous_comment_id),
                        'tone': decision['tone']})

    attempt = AICommentAttempt(post_id=post.id, requested_by=requester.id,
                               status='started')
    db.session.add(attempt)
    db.session.commit()
    try:
        content = service.generate_post_comment(post, decision['tone'])
        post = db.session.get(Post, post.id)
        if not post or post.status != 'published':
            raise AICommentError('post_unavailable', '博文已不可用。')
        if content is None:
            post.ai_comment_status = ('generated' if previous_comment_id
                                      else 'skipped')
            post.ai_comment_error = ('regenerate_model_skip'
                                     if previous_comment_id else 'model_skip')
            attempt.status = 'skipped'
            post.ai_comment_updated_at = utcnow()
            db.session.commit()
            return jsonify({'status': post.ai_comment_status,
                            'retained_previous': bool(previous_comment_id)})
        bot = ensure_ai_bot()
        duplicate = Comment.query.filter_by(
            post_id=post.id, is_ai_generated=True, status='visible').first()
        if duplicate and not previous_comment_id:
            post.ai_comment_status = 'generated'
            post.ai_comment_id = duplicate.id
            attempt.status = 'duplicate'
            db.session.commit()
            return jsonify({'status': 'generated'})
        comment = Comment(post_id=post.id, author_id=bot.id,
                          content=content, is_ai_generated=True)
        db.session.add(comment)
        db.session.flush()
        if duplicate and previous_comment_id:
            duplicate.status = 'deleted'
        post.ai_comment_status = 'generated'
        post.ai_comment_id = comment.id
        post.ai_comment_error = ''
        post.ai_comment_updated_at = utcnow()
        attempt.status = 'success'
        db.session.commit()
        return jsonify({'status': 'generated',
                        'comment': _serialize_comment(comment),
                        'count': _comment_counts([post.id]).get(post.id, 0)}), 201
    except AICommentError as error:
        db.session.rollback()
        post = db.session.get(Post, post_id)
        attempt = db.session.get(AICommentAttempt, attempt.id)
        if post:
            post.ai_comment_status = ('generated' if previous_comment_id
                                      else 'failed')
            post.ai_comment_error = (
                ('regenerate_' + error.code) if previous_comment_id
                else error.code)[:255]
            post.ai_comment_updated_at = utcnow()
        if attempt:
            attempt.status = 'failed'
            attempt.error_code = error.code[:80]
        db.session.commit()
        LOGGER.warning('AI comment failed for post %s: %s', post_id, error.code)
        return jsonify({'error': 'AI评论暂时没有出现。',
                        'status': ('generated' if previous_comment_id
                                   else 'failed'),
                        'retained_previous': bool(previous_comment_id)}), 503
    except Exception:
        db.session.rollback()
        post = db.session.get(Post, post_id)
        attempt = db.session.get(AICommentAttempt, attempt.id)
        if post:
            post.ai_comment_status = ('generated' if previous_comment_id
                                      else 'failed')
            post.ai_comment_error = ('regenerate_internal_error'
                                     if previous_comment_id
                                     else 'internal_error')
            post.ai_comment_updated_at = utcnow()
        if attempt:
            attempt.status = 'failed'
            attempt.error_code = 'internal_error'
        db.session.commit()
        LOGGER.exception('Unexpected AI comment failure for post %s', post_id)
        return jsonify({'error': 'AI评论暂时没有出现。',
                        'status': ('generated' if previous_comment_id
                                   else 'failed'),
                        'retained_previous': bool(previous_comment_id)}), 503


@main.route('/comment/<int:comment_id>/delete', methods=['POST'])
@login_required
def delete_comment(comment_id):
    comment = db.session.get(Comment, comment_id)
    if not comment:
        abort(404)
    
    user = db.session.get(User, session['user_id'])
    is_author = comment.author_id == session['user_id']
    is_admin = user and user.role == 'admin'
    
    if not is_author and not is_admin:
        abort(403)
    
    post_id = comment.post_id
    comment.status = 'deleted'
    db.session.commit()
    flash('评论已删除。')
    return redirect(url_for('main.view_post', post_id=post_id))
