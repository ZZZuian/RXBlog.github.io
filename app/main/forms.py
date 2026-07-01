import re
from markupsafe import Markup
from flask_wtf import FlaskForm as Form
from flask_wtf.file import MultipleFileField
from wtforms import (SelectField, StringField, SubmitField, TextAreaField,
                     ValidationError)
from wtforms.validators import DataRequired, Length

from app.taxonomy import CATEGORY_CHOICES, DEFAULT_CATEGORY


class RawEntryForm(Form):
    raw_entry = StringField('发布一条社区动态：', validators=[DataRequired()])
    submit = SubmitField('发布记录')


# Custom validators

def no_hashtags(form, field):
    if bool(re.search(r'#', field.data)):
        raise ValidationError(Markup("标签不需要以 \
            <code>#</code> 开头。"))

class CommentForm(Form):
    content = TextAreaField('评论内容', validators=[DataRequired(), Length(max=500)])
    submit = SubmitField('发表评论')


class DeleteEntryForm(Form):
    submit = SubmitField('删除记录')


class PostForm(Form):
    title = StringField('标题', validators=[DataRequired(), Length(max=200)])
    content = TextAreaField('正文', validators=[DataRequired(), Length(max=20000)])
    category = SelectField('内容分区', choices=CATEGORY_CHOICES,
                           default=DEFAULT_CATEGORY,
                           validators=[DataRequired()])
    tags = StringField('标签（用逗号分隔，无需输入 #）',
                       validators=[Length(max=300), no_hashtags])
    images = MultipleFileField('图片（最多 9 张，单张不超过 5MB）')
    music_track_id = SelectField('博文背景音乐', coerce=int,
                                 choices=[(0, '不设置，自动选择作者或默认音乐')],
                                 default=0)
    submit = SubmitField('发布博文')


class PinPostForm(Form):
    submit = SubmitField('置顶主页')
