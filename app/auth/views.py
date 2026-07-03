from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import flash, redirect, render_template, request, session, url_for

from app.decorators import login_required
from app.accounts import allocate_account
from app.details import get_details
from app.extensions import db
from app.models import Profile, User
from . import auth, pwd_context
from .forms import (ChangeAccountForm, ChangePasswordForm, LoginForm,
                    RegistrationForm, ResetPasswordForm)


def _safe_next_url(target):
    if not target:
        return None
    parsed = urlsplit(target)
    return target if not parsed.netloc and not parsed.scheme else None


@auth.route('/login', methods=['GET', 'POST'])
def login():
    details = get_details()
    if not User.query.filter(User.is_bot.is_(False)).first():
        flash('您需要先注册。')
        return redirect(url_for('auth.register'))
    if session.get('logged_in'):
        return redirect(url_for('main.browse_all_entries'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.account.data).first()
        if user.is_bot:
            flash('机器人账号不能登录前台。')
            return render_template('login.html', form=form, details=details), 403
        if user.status != 'active':
            flash('该账号已被停用。')
            return render_template('login.html', form=form, details=details), 403
        session.clear()
        session['logged_in'] = True
        session['user_id'] = user.id
        user.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        return redirect(_safe_next_url(request.args.get('next')) or
                        url_for('main.browse_all_entries'))
    return render_template('login.html', form=form, details=details)


@auth.route('/logout')
@login_required
def logout():
    session.clear()
    flash('您已成功退出登录。')
    return redirect(url_for('main.browse_all_entries'))


@auth.route('/register', methods=['GET', 'POST'])
def register():
    details = get_details()
    form = RegistrationForm()
    if form.validate_on_submit():
        role = ('admin' if not User.query.filter(
            User.is_bot.is_(False)).first() else 'user')
        account = allocate_account()
        user = User(username=account,
                    password_hash=pwd_context.hash(form.password.data),
                    role=role, status='active')
        user.profile = Profile(nickname=form.nickname.data)
        db.session.add(user)
        db.session.commit()
        flash('注册成功，您的账号是 {}，请妥善保存。'.format(account))
        return redirect(url_for('auth.login'))
    return render_template('register.html', form=form, details=details,
                           register=True)


@auth.route('/reset_password', methods=['GET', 'POST'])
def request_reset():
    details = get_details()
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.account.data).first()
        user.password_hash = pwd_context.hash(form.new_password.data)
        db.session.commit()
        flash('密码已重置成功，请使用新密码登录。')
        return redirect(url_for('auth.login'))
    return render_template('reset_password.html', form=form, details=details)


@auth.route('/change_account', methods=['GET', 'POST'])
@login_required
def change_account():
    details = get_details()
    form = ChangeAccountForm()
    if form.validate_on_submit():
        user = db.session.get(User, session['user_id'])
        user.username = form.new_account.data
        db.session.commit()
        flash('您的账号已更新。')
        return redirect(url_for('admin.view_admin'))
    return render_template('change_account.html', form=form, details=details)


@auth.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    details = get_details()
    form = ChangePasswordForm()
    if form.validate_on_submit():
        user = db.session.get(User, session['user_id'])
        user.password_hash = pwd_context.hash(form.new_password.data)
        db.session.commit()
        flash('您的密码已更新。')
        return redirect(url_for('admin.view_admin'))
    return render_template('change_password.html', form=form, details=details)
