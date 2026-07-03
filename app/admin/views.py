from flask import (abort, current_app, flash, redirect, render_template,
                   request, session, url_for)
from sqlalchemy import func, select

from app.decorators import admin_required, login_required
from app.details import get_details
from app.extensions import db
from app.models import AICommentAttempt, Comment, Post, SiteSetting, User
from . import admin
from .forms import RenameAuthorForm, RenameChronofileForm


@admin.route('/')
@login_required
def view_admin():
    user = db.session.get(User, session['user_id'])
    ai_posts = []
    ai_comment_count = 0
    ai_call_count = 0
    if user.role == 'admin':
        ai_posts = db.session.scalars(select(Post).where(
            Post.ai_comment_status != 'none').order_by(
                Post.ai_comment_updated_at.desc()).limit(20)).all()
        ai_comment_count = db.session.scalar(select(func.count(Comment.id)).where(
            Comment.is_ai_generated.is_(True))) or 0
        ai_call_count = db.session.scalar(select(
            func.count(AICommentAttempt.id))) or 0
    enabled_setting = db.session.get(SiteSetting, 'ai_comment_enabled')
    probability_setting = db.session.get(SiteSetting, 'ai_comment_probability')
    return render_template(
        'admin.html', details=get_details(), user=user, ai_posts=ai_posts,
        ai_comment_count=ai_comment_count, ai_call_count=ai_call_count,
        ai_comment_enabled=(enabled_setting.value.lower() in
                            ('1', 'true', 'yes', 'on') if enabled_setting else
                            bool(current_app.config.get('AI_COMMENT_ENABLED', True))),
        ai_comment_probability=(probability_setting.value if
                                probability_setting else
                                current_app.config.get(
                                    'AI_COMMENT_PROBABILITY', 0.4)))


@admin.route('/ai-comments/settings', methods=['POST'])
@admin_required
def update_ai_comment_settings():
    enabled = request.form.get('enabled') == 'on'
    try:
        probability = max(0.0, min(1.0, float(
            request.form.get('probability', '0.4'))))
    except ValueError:
        flash('自动评论概率必须是 0 到 1 之间的数字。')
        return redirect(url_for('admin.view_admin'))
    for key, value in (
            ('ai_comment_enabled', 'true' if enabled else 'false'),
            ('ai_comment_probability', str(probability))):
        setting = db.session.get(SiteSetting, key)
        if not setting:
            setting = SiteSetting(key=key)
            db.session.add(setting)
        setting.value = value
        setting.updated_by = session['user_id']
    db.session.commit()
    flash('AI评论设置已更新。')
    return redirect(url_for('admin.view_admin'))


@admin.route('/ai-comments/posts/<int:post_id>/toggle', methods=['POST'])
@admin_required
def toggle_post_ai_comment(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        abort(404)
    post.allow_ai_comment = not post.allow_ai_comment
    db.session.commit()
    flash('该博文已{}AI评论。'.format(
        '允许' if post.allow_ai_comment else '禁止'))
    return redirect(url_for('admin.view_admin'))


@admin.route('/rename_chronofile', methods=['GET', 'POST'])
@admin_required
def rename_chronofile():
    form = RenameChronofileForm()
    if form.validate_on_submit():
        setting = db.session.get(SiteSetting, 'site_name')
        if not setting:
            setting = SiteSetting(key='site_name')
            db.session.add(setting)
        setting.value = form.new_name.data.strip()
        setting.updated_by = session['user_id']
        db.session.commit()
        flash('平台名称已更新。')
        return redirect(url_for('admin.view_admin'))
    return render_template('rename_chronofile.html', form=form,
                           details=get_details())


@admin.route('/rename_author', methods=['GET', 'POST'])
@login_required
def rename_author():
    form = RenameAuthorForm()
    if form.validate_on_submit():
        user = db.session.get(User, session['user_id'])
        user.profile.nickname = form.new_name.data.strip()
        db.session.commit()
        flash('个人昵称已更新。')
        return redirect(url_for('admin.view_admin'))
    return render_template('rename_author.html', form=form,
                           details=get_details())
