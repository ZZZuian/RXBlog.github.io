import re
from flask import Markup
from flask_wtf import FlaskForm as Form
from wtforms import StringField, SubmitField, ValidationError
from wtforms.validators import DataRequired


class RawEntryForm(Form):
    raw_entry = StringField('在时间记录本中添加新记录：', validators=[DataRequired()])
    submit = SubmitField('发布记录')


# Custom validators

def no_hashtags(form, field):
    if bool(re.search(r'#', field.data)):
        raise ValidationError(Markup("标签不需要以 \
            <code>#</code> 开头。"))

def use_commas(form, field):
    tags = field.data.split(' ')
    items = len(tags)
    print(items)
    if items != 1:
        count = 1
        for i in tags:
            print(i)
            print(i[-1])
            if i[-1] == ',':
                print(i[-1])
                count += 1
        print(count)
        if count < items:
            raise ValidationError(Markup('请使用逗号分隔标签，格式如下：<code>tag1, tag2</code>。'))


class EditEntryForm(Form):
    new_entry = StringField('编辑记录内容：', validators=[DataRequired()])
    new_tags = StringField("编辑标签：（用逗号分隔，无需使用 #）", \
        validators=[no_hashtags, use_commas])
    submit = SubmitField('保存修改')