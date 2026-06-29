from functools import wraps

from flask import abort, flash, redirect, request, session, url_for

from app.extensions import db
from app.models import User


def login_required(view):
    @wraps(view)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        user = db.session.get(User, user_id) if user_id else None
        if not session.get('logged_in') or not user:
            flash('请先登录。')
            return redirect(url_for('auth.login', next=request.url))
        if user.status != 'active':
            session.clear()
            abort(403)
        return view(*args, **kwargs)
    return decorated_function


def admin_required(view):
    @wraps(view)
    @login_required
    def decorated_function(*args, **kwargs):
        user = db.session.get(User, session['user_id'])
        if user.role != 'admin':
            abort(403)
        return view(*args, **kwargs)
    return decorated_function
