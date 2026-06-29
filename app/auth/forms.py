import re
from flask import session
from flask_wtf import FlaskForm as Form
from wtforms import PasswordField, StringField, SubmitField, ValidationError
from wtforms.validators import DataRequired, Length, Email, EqualTo
from app.db import *
from .views import pwd_context


# 基本验证
def account_exists(form, field):
    user = get_record('auth', Query().email == field.data)
    if not user:
        raise ValidationError('请先注册账号。')


def email_exists(form, field):
    user = get_record('auth', Query().email == field.data)
    if not user:
        raise ValidationError('请确认您输入的邮箱地址正确。')


def authorized(form, field):
    '''Verify user through password.'''
    user = get_table('auth').get(eid=session.get('user_id'))
    if not pwd_context.verify(field.data, user['password_hash']):
        raise ValidationError('登录凭据无效，请重试。')


def has_digits(form, field):
    if not bool(re.search(r'\d', field.data)):
        raise ValidationError('密码必须包含至少一个数字。')


def has_special_char(form, field):
    if not bool(re.search(r'[^\w\*]', field.data)):
        raise ValidationError('密码必须包含至少一个特殊字符。')


class PasswordCorrect(object):
    '''Verify email/password combo before validating form.'''
    def __init__(self, fieldname):
        self.fieldname = fieldname

    def __call__(self, form, field):
        try:
            email = form[self.fieldname]
        except KeyError:
            raise ValidationError(field.gettext("无效的字段名 '%s'。") \
                % self.fieldname)
        user = get_record('auth', Query().email == email.data)
        if not pwd_context.verify(field.data, user['password_hash']):
            raise ValidationError('密码错误，请重试。')


class LoginForm(Form):
    email = StringField('邮箱：', validators=[DataRequired(), Email(), \
        account_exists])
    password = PasswordField('密码：', validators=[DataRequired(), \
        PasswordCorrect('email')])
    submit = SubmitField('登录')


class RegistrationForm(Form):
    email = StringField('请输入邮箱地址：', \
        validators=[DataRequired(), Email()])
    password = PasswordField('请输入密码：' +\
        '（最少6位，须包含数字和特殊字符）', \
        validators=[DataRequired(), Length(min=6), has_digits, has_special_char])
    submit = SubmitField('创建账号')


class ChangeEmailForm(Form):
    password = PasswordField('请输入当前密码：', validators=[DataRequired(), \
        authorized])
    new_email = StringField('新邮箱地址：', \
        validators=[DataRequired(), Email(), EqualTo('verify_email', \
        message='两次输入的邮箱不一致')])
    verify_email = StringField('再次输入新邮箱地址：', \
        validators=[DataRequired(), Email()])
    submit = SubmitField('修改邮箱')


class ChangePasswordForm(Form):
    current_password = PasswordField('当前密码：', \
        validators=[DataRequired(), authorized])
    new_password = PasswordField('新密码：' +\
        '（最少6位，须包含数字和特殊字符）', \
        validators=[DataRequired(), Length(min=6), EqualTo('verify_password', \
        message='两次输入的新密码不一致。'), has_digits, has_special_char])
    verify_password = PasswordField('再次输入新密码：', \
        validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('修改密码')


class ResetPasswordForm(Form):
    email = StringField('您注册时使用的邮箱地址：',
        validators=[DataRequired(), Email(), email_exists])
    new_password = PasswordField('新密码：' +\
        '（最少6位，须包含数字和特殊字符）', \
        validators=[DataRequired(), Length(min=6), EqualTo('verify_password', \
        message='两次输入的新密码不一致。'), has_digits, has_special_char])
    verify_password = PasswordField('再次输入新密码：', \
        validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('重置密码')


class SetNewPasswordForm(Form):
    new_password = PasswordField('新密码：' +\
        '（最少6位，须包含数字和特殊字符）', \
        validators=[DataRequired(), Length(min=6), EqualTo('verify_password', \
        message='两次输入的新密码不一致。'), has_digits, has_special_char])
    verify_password = PasswordField('再次输入新密码：', \
        validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('设置新密码')