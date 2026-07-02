from datetime import datetime, timedelta, timezone

from flask import (abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from app.decorators import login_required
from app.details import get_details
from app.extensions import db
from app.models import Comment, Post, PostImage, PostLike, User
from app.parse import parse_input
from app.taxonomy import category_meta, normalize_tags
from app.time_utils import format_china_time
from app.uploads import (UploadValidationError, cleanup_paths,
                         media_absolute_path, prepare_images, store_images)
from . import main
from .forms import (CommentForm, DeleteEntryForm, PinPostForm, PostForm,
                    RawEntryForm)


PER_PAGE = 10


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


def _is_admin(user_id=None):
    user = db.session.get(User, user_id or session.get('user_id'))
    return bool(user and user.role == 'admin' and user.status == 'active')


def _can_manage_post(post):
    return post.author_id == session.get('user_id') or _is_admin()


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
        try:
            prepared = prepare_images(form.images.data or [])
            post = Post(author_id=session['user_id'], title=form.title.data.strip(),
                        content=form.content.data.strip(), board='public',
                        category=form.category.data,
                        tags=_parse_tags(form.tags.data), status='published')
            db.session.add(post)
            db.session.flush()
            created_paths = store_images(post, session['user_id'], prepared)
            db.session.commit()
            flash('博文发布成功。')
            return redirect(url_for('main.view_post', post_id=post.id))
        except UploadValidationError as error:
            db.session.rollback()
            cleanup_paths(created_paths)
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
                           page_post_author_id=post.author_id)


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
        selected_ids = {int(value) for value in
                        request.form.getlist('remove_image_ids')
                        if value.isdigit()}
        remove_items = [item for item in post.image_items
                        if item.id in selected_ids]
        existing_count = (len(post.image_items) + len(post.images or []) -
                          len(remove_items))
        created_paths = []
        removed_paths = [media_absolute_path(item.media)
                         for item in remove_items]
        try:
            prepared = prepare_images(form.images.data or [], existing_count)
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
            created_paths = store_images(post, session['user_id'], prepared)
            db.session.commit()
            cleanup_paths(removed_paths)
            flash('博文已更新。')
            return redirect(url_for('main.view_post', post_id=post.id))
        except UploadValidationError as error:
            db.session.rollback()
            cleanup_paths(created_paths)
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
    user = db.session.get(User, session['user_id'])
    avatar = (user.profile.avatar if user.profile and user.profile.avatar
              else 'images/default-avatar.jpg')
    comment_count = db.session.scalar(
        select(func.count(Comment.id)).where(
            Comment.post_id == post.id, Comment.status == 'visible')) or 0
    return jsonify({
        'comment': {
            'id': comment.id,
            'content': comment.content,
            'nickname': user.display_name,
            'profile_url': url_for('user.view_user_profile', user_id=user.id),
            'avatar_url': url_for('static', filename=avatar),
            'created_at': format_china_time(comment.created_at)
        },
        'count': comment_count
    }), 201


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
