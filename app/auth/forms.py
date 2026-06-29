import re

from flask import session
from flask_wtf import FlaskForm as Form
from wtforms import PasswordField, StringField, SubmitField, ValidationError
from wtforms.validators import DataRequired, EqualTo, Length

from app.extensions import db
from app.models import Profile, User
from . import pwd_context


def normalize_account(value):
    return value.strip().lower() if value else value


def account_exists(form, field):
    if not User.query.filter_by(username=field.data).first():
        raise ValidationError('账号不存在，请先注册。')


def account_available(form, field):
    if User.query.filter_by(username=field.data).first():
        raise ValidationError('该账号已被注册。')


def valid_account(form, field):
    if not re.fullmatch(r'[\w.-]+', field.data, re.UNICODE):
        raise ValidationError('账号只能包含文字、字母、数字、下划线、点和连字符。')


def nickname_available(form, field):
    if Profile.query.filter_by(nickname=field.data.strip()).first():
        raise ValidationError('该昵称已被使用。')


def authorized(form, field):
    user = db.session.get(User, session.get('user_id'))
    if not user or not pwd_context.verify(field.data, user.password_hash):
        raise ValidationError('登录凭据无效，请重试。')


def has_digits(form, field):
    if not re.search(r'\d', field.data):
        raise ValidationError('密码必须包含至少一个数字。')


def has_special_char(form, field):
    if not re.search(r'[^\w\*]', field.data):
        raise ValidationError('密码必须包含至少一个特殊字符。')


class PasswordCorrect:
    def __init__(self, fieldname):
        self.fieldname = fieldname

    def __call__(self, form, field):
        try:
            account = form[self.fieldname]
        except KeyError:
            raise ValidationError("无效的字段名 '%s'。" % self.fieldname)
        user = User.query.filter_by(username=account.data).first()
        if not user or not pwd_context.verify(field.data, user.password_hash):
            raise ValidationError('账号或密码错误，请重试。')


class LoginForm(Form):
    account = StringField('账号：', filters=[normalize_account],
                          validators=[DataRequired(), Length(min=3, max=254),
                                      account_exists])
    password = PasswordField('密码：', validators=[DataRequired(),
                                                   PasswordCorrect('account')])
    submit = SubmitField('登录')


class RegistrationForm(Form):
    nickname = StringField('昵称：', filters=[lambda value: value.strip()
                           if value else value], validators=[DataRequired(),
                           Length(min=2, max=30), nickname_available])
    account = StringField('账号：', filters=[normalize_account],
                          validators=[DataRequired(), Length(min=3, max=30),
                                      valid_account, account_available])
    password = PasswordField('请输入密码：（最少6位，须包含数字和特殊字符）',
        validators=[DataRequired(), Length(min=6), has_digits, has_special_char])
    submit = SubmitField('创建账号')


class ChangeAccountForm(Form):
    password = PasswordField('请输入当前密码：', validators=[DataRequired(),
                                                            authorized])
    new_account = StringField('新账号：', filters=[normalize_account],
        validators=[DataRequired(), Length(min=3, max=30), valid_account,
                    account_available, EqualTo('verify_account',
                    message='两次输入的账号不一致')])
    verify_account = StringField('再次输入新账号：', filters=[normalize_account],
        validators=[DataRequired(), Length(min=3, max=30), valid_account])
    submit = SubmitField('修改账号')


class ChangePasswordForm(Form):
    current_password = PasswordField('当前密码：',
        validators=[DataRequired(), authorized])
    new_password = PasswordField('新密码：（最少6位，须包含数字和特殊字符）',
        validators=[DataRequired(), Length(min=6), EqualTo('verify_password',
        message='两次输入的新密码不一致。'), has_digits, has_special_char])
    verify_password = PasswordField('再次输入新密码：',
        validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('修改密码')


class ResetPasswordForm(Form):
    account = StringField('您注册时使用的账号：', filters=[normalize_account],
        validators=[DataRequired(), Length(min=3, max=254), account_exists])
    new_password = PasswordField('新密码：（最少6位，须包含数字和特殊字符）',
        validators=[DataRequired(), Length(min=6), EqualTo('verify_password',
        message='两次输入的新密码不一致。'), has_digits, has_special_char])
    verify_password = PasswordField('再次输入新密码：',
        validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('重置密码')


class SetNewPasswordForm(Form):
    new_password = PasswordField('新密码：（最少6位，须包含数字和特殊字符）',
        validators=[DataRequired(), Length(min=6), EqualTo('verify_password',
        message='两次输入的新密码不一致。'), has_digits, has_special_char])
    verify_password = PasswordField('再次输入新密码：',
        validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('设置新密码')
