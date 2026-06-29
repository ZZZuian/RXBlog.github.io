from flask import flash, redirect, render_template, session, url_for

from app.decorators import admin_required, login_required
from app.details import get_details
from app.extensions import db
from app.models import SiteSetting, User
from . import admin
from .forms import RenameAuthorForm, RenameChronofileForm


@admin.route('/')
@login_required
def view_admin():
    user = db.session.get(User, session['user_id'])
    return render_template('admin.html', details=get_details(), user=user)


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
